# coding: utf-8

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta as td, timezone

from cronsim import CronSim
from django.conf import settings
from django.core.signing import TimestampSigner
from django.db import models
from django.urls import reverse
from django.utils.timezone import now
from django.utils.text import slugify
from hc.accounts.models import Project
from hc.api import transports
from hc.lib import emails
from hc.lib.date import month_boundaries
from hc.lib.s3 import get_object, put_object, remove_objects

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


STATUSES = (("up", "Up"), ("down", "Down"), ("new", "New"), ("paused", "Paused"))
DEFAULT_TIMEOUT = td(days=1)
DEFAULT_GRACE = td(hours=1)
NEVER = datetime(3000, 1, 1, tzinfo=timezone.utc)
CHECK_KINDS = (("simple", "Simple"), ("cron", "Cron"))
# max time between start and ping where we will consider both events related:
MAX_DELTA = td(hours=24)

CHANNEL_KINDS = (
    ("email", "Email"),
    ("webhook", "Webhook"),
    ("hipchat", "HipChat"),
    ("slack", "Slack"),
    ("pd", "PagerDuty"),
    ("pagertree", "PagerTree"),
    ("pagerteam", "Pager Team"),
    ("po", "Pushover"),
    ("pushbullet", "Pushbullet"),
    ("opsgenie", "Opsgenie"),
    ("victorops", "Splunk On-Call"),
    ("discord", "Discord"),
    ("telegram", "Telegram"),
    ("sms", "SMS"),
    ("zendesk", "Zendesk"),
    ("trello", "Trello"),
    ("matrix", "Matrix"),
    ("whatsapp", "WhatsApp"),
    ("apprise", "Apprise"),
    ("mattermost", "Mattermost"),
    ("msteams", "Microsoft Teams"),
    ("shell", "Shell Command"),
    ("zulip", "Zulip"),
    ("spike", "Spike"),
    ("call", "Phone Call"),
    ("linenotify", "LINE Notify"),
    ("signal", "Signal"),
)

PO_PRIORITIES = {-2: "lowest", -1: "low", 0: "normal", 1: "high", 2: "emergency"}


def isostring(dt):
    """Convert the datetime to ISO 8601 format with no microseconds. """

    if dt:
        return dt.replace(microsecond=0).isoformat()


class Check(models.Model):
    name = models.CharField(max_length=100, blank=True)
    slug = models.CharField(max_length=100, blank=True)
    tags = models.CharField(max_length=500, blank=True)
    code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    desc = models.TextField(blank=True)
    project = models.ForeignKey(Project, models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=10, default="simple", choices=CHECK_KINDS)
    timeout = models.DurationField(default=DEFAULT_TIMEOUT)
    grace = models.DurationField(default=DEFAULT_GRACE)
    schedule = models.CharField(max_length=100, default="* * * * *")
    tz = models.CharField(max_length=36, default="UTC")
    subject = models.CharField(max_length=200, blank=True)
    subject_fail = models.CharField(max_length=200, blank=True)
    methods = models.CharField(max_length=30, blank=True)
    manual_resume = models.BooleanField(default=False)

    n_pings = models.IntegerField(default=0)
    last_ping = models.DateTimeField(null=True, blank=True)
    last_start = models.DateTimeField(null=True, blank=True)
    last_duration = models.DurationField(null=True, blank=True)
    last_ping_was_fail = models.BooleanField(default=False)
    has_confirmation_link = models.BooleanField(default=False)
    alert_after = models.DateTimeField(null=True, blank=True, editable=False)
    status = models.CharField(max_length=6, choices=STATUSES, default="new")

    class Meta:
        indexes = [
            # Index for the alert_after field. Excludes rows with status=down.
            # Used in the sendalerts management command.
            models.Index(
                fields=["alert_after"],
                name="api_check_aa_not_down",
                condition=~models.Q(status="down"),
            ),
            models.Index(fields=["project_id", "slug"], name="api_check_project_slug"),
        ]

    def __str__(self):
        return "%s (%d)" % (self.name or self.code, self.id)

    def name_then_code(self):
        if self.name:
            return self.name

        return str(self.code)

    def url(self):
        """ Return check's ping url in user's preferred style.

        Note: this method reads self.project. If project is not loaded already,
        this causes a SQL query.

        """

        if self.project_id and self.project.show_slugs:
            if not self.slug:
                return None

            # If ping_key is not set, use dummy placeholder
            key = self.project.ping_key or "{ping_key}"
            return settings.PING_ENDPOINT + key + "/" + self.slug

        return settings.PING_ENDPOINT + str(self.code)

    def details_url(self):
        return settings.SITE_ROOT + reverse("hc-details", args=[self.code])

    def cloaked_url(self):
        return settings.SITE_ROOT + reverse("hc-uncloak", args=[self.unique_key])

    def email(self):
        return "%s@%s" % (self.code, settings.PING_EMAIL_DOMAIN)

    def clamped_last_duration(self):
        if self.last_duration and self.last_duration < MAX_DELTA:
            return self.last_duration

    def set_name_slug(self, name):
        self.name = name
        self.slug = slugify(name)

    def get_grace_start(self, with_started=True):
        """ Return the datetime when the grace period starts.

        If the check is currently new, paused or down, return None.

        """

        # NEVER is a constant sentinel value (year 3000).
        # Using None instead would make the min() logic clunky.
        result = NEVER

        if self.kind == "simple" and self.status == "up":
            result = self.last_ping + self.timeout
        elif self.kind == "cron" and self.status == "up":
            # The complex case, next ping is expected based on cron schedule.
            # Don't convert to naive datetimes (and so avoid ambiguities around
            # DST transitions). cronsim will handle the timezone-aware datetimes.
            last_local = self.last_ping.astimezone(ZoneInfo(self.tz))
            result = next(CronSim(self.schedule, last_local))

        if with_started and self.last_start and self.status != "down":
            result = min(result, self.last_start)

        if result != NEVER:
            return result

    def going_down_after(self):
        """ Return the datetime when the check goes down.

        If the check is new or paused, and not currently running, return None.
        If the check is already down, also return None.

        """

        grace_start = self.get_grace_start()
        if grace_start is not None:
            return grace_start + self.grace

    def get_status(self, with_started=False):
        """ Return current status for display. """

        frozen_now = now()

        if self.last_start:
            if frozen_now >= self.last_start + self.grace:
                return "down"
            elif with_started:
                return "started"

        if self.status in ("new", "paused", "down"):
            return self.status

        grace_start = self.get_grace_start(with_started=with_started)
        grace_end = grace_start + self.grace
        if frozen_now >= grace_end:
            return "down"

        if frozen_now >= grace_start:
            return "grace"

        return "up"

    def assign_all_channels(self):
        channels = Channel.objects.filter(project=self.project)
        self.channel_set.set(channels)

    def tags_list(self):
        return [t.strip() for t in self.tags.split(" ") if t.strip()]

    def matches_tag_set(self, tag_set):
        return tag_set.issubset(self.tags_list())

    def channels_str(self):
        """ Return a comma-separated string of assigned channel codes. """

        # self.channel_set may already be prefetched.
        # Sort in python to make sure we do't run additional queries
        codes = [str(channel.code) for channel in self.channel_set.all()]
        return ",".join(sorted(codes))

    @property
    def unique_key(self):
        code_half = self.code.hex[:16]
        return hashlib.sha1(code_half.encode()).hexdigest()

    def to_dict(self, readonly=False):

        result = {
            "name": self.name,
            "slug": self.slug,
            "tags": self.tags,
            "desc": self.desc,
            "grace": int(self.grace.total_seconds()),
            "n_pings": self.n_pings,
            "status": self.get_status(with_started=True),
            "last_ping": isostring(self.last_ping),
            "next_ping": isostring(self.get_grace_start()),
            "manual_resume": self.manual_resume,
            "methods": self.methods,
        }

        if self.last_duration:
            result["last_duration"] = int(self.last_duration.total_seconds())

        if readonly:
            result["unique_key"] = self.unique_key
        else:
            update_rel_url = reverse("hc-api-single", args=[self.code])
            pause_rel_url = reverse("hc-api-pause", args=[self.code])

            result["ping_url"] = settings.PING_ENDPOINT + str(self.code)
            result["update_url"] = settings.SITE_ROOT + update_rel_url
            result["pause_url"] = settings.SITE_ROOT + pause_rel_url
            result["channels"] = self.channels_str()

        if self.kind == "simple":
            result["timeout"] = int(self.timeout.total_seconds())
        elif self.kind == "cron":
            result["schedule"] = self.schedule
            result["tz"] = self.tz

        return result

    def ping(self, remote_addr, scheme, method, ua, body, action, exitstatus=None):
        frozen_now = now()

        if self.status == "paused" and self.manual_resume:
            action = "ign"

        if action == "start":
            self.last_start = frozen_now
            # Don't update "last_ping" field.
        elif action == "ign":
            pass
        else:
            self.last_ping = frozen_now
            if self.last_start:
                self.last_duration = self.last_ping - self.last_start
                self.last_start = None
            else:
                self.last_duration = None

            new_status = "down" if action == "fail" else "up"
            if self.status != new_status:
                flip = Flip(owner=self)
                flip.created = self.last_ping
                flip.old_status = self.status
                flip.new_status = new_status
                flip.save()

                self.status = new_status

        self.alert_after = self.going_down_after()
        self.n_pings = models.F("n_pings") + 1
        self.has_confirmation_link = "confirm" in body.decode(errors="replace").lower()
        self.save()
        self.refresh_from_db()

        ping = Ping(owner=self)
        ping.n = self.n_pings
        ping.created = frozen_now
        if action in ("start", "fail", "ign"):
            ping.kind = action

        ping.remote_addr = remote_addr
        ping.scheme = scheme
        ping.method = method
        # If User-Agent is longer than 200 characters, truncate it:
        ping.ua = ua[:200]
        if len(body) > 100 and settings.S3_BUCKET:
            ping.object_size = len(body)
            put_object(self.code, ping.n, body)
        else:
            ping.body_raw = body
        ping.exitstatus = exitstatus
        ping.save()

        # Every 100 received pings, prune old pings and notifications:
        if self.n_pings % 100 == 0:
            self.prune()

    def prune(self):
        """ Remove old pings and notifications. """

        threshold = self.n_pings - self.project.owner_profile.ping_log_limit

        # Remove ping bodies from object storage
        if settings.S3_BUCKET:
            remove_objects(self.code, threshold)

        # Remove ping objects from db
        self.ping_set.filter(n__lte=threshold).delete()

        try:
            ping = self.ping_set.earliest("id")
            self.notification_set.filter(created__lt=ping.created).delete()
        except Ping.DoesNotExist:
            pass

    def downtimes(self, months):
        """ Calculate the number of downtimes and downtime minutes per month.

        Returns a list of (datetime, downtime_in_secs, number_of_outages) tuples.

        """

        def monthkey(dt):
            return dt.year, dt.month

        # Datetimes of the first days of months we're interested in. Ascending order.
        boundaries = month_boundaries(months=months)

        # Will accumulate totals here.
        # (year, month) -> [datetime, total_downtime, number_of_outages]
        totals = {monthkey(b): [b, td(), 0] for b in boundaries}

        # A list of flips and month boundaries
        events = [(b, "---") for b in boundaries]
        q = self.flip_set.filter(created__gt=min(boundaries))
        for pair in q.values_list("created", "old_status"):
            events.append(pair)

        # Iterate through flips and month boundaries in reverse order,
        # and for each "down" event increase the counters in `totals`.
        dt, status = now(), self.status
        for prev_dt, prev_status in sorted(events, reverse=True):
            if status == "down":
                delta = dt - prev_dt
                totals[monthkey(prev_dt)][1] += delta
                totals[monthkey(prev_dt)][2] += 1

            dt = prev_dt
            if prev_status != "---":
                status = prev_status

        # Set counters to None for months when the check didn't exist yet
        for ym in totals:
            if ym < monthkey(self.created):
                totals[ym][1] = None
                totals[ym][2] = None

        return sorted(totals.values())

    def past_downtimes(self):
        """ Return downtime summary for two previous months. """

        return self.downtimes(3)[:-1]


class Ping(models.Model):
    id = models.BigAutoField(primary_key=True)
    n = models.IntegerField(null=True)
    owner = models.ForeignKey(Check, models.CASCADE)
    created = models.DateTimeField(default=now)
    kind = models.CharField(max_length=6, blank=True, null=True)
    scheme = models.CharField(max_length=10, default="http")
    remote_addr = models.GenericIPAddressField(blank=True, null=True)
    method = models.CharField(max_length=10, blank=True)
    ua = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True, null=True)
    body_raw = models.BinaryField(null=True)
    object_size = models.IntegerField(null=True)
    exitstatus = models.SmallIntegerField(null=True)

    def to_dict(self):
        return {
            "type": self.kind or "success",
            "date": self.created.isoformat(),
            "n": self.n,
            "scheme": self.scheme,
            "remote_addr": self.remote_addr,
            "method": self.method,
            "ua": self.ua,
        }

    def has_body(self):
        if self.body or self.body_raw or self.object_size:
            return True

        return False

    def get_body(self):
        if self.body:
            return self.body
        if self.object_size:
            body_raw = get_object(self.owner.code, self.n)
            if body_raw:
                return body_raw.decode(errors="replace")
        if self.body_raw:
            return bytes(self.body_raw).decode(errors="replace")


class Channel(models.Model):
    name = models.CharField(max_length=100, blank=True)
    code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    project = models.ForeignKey(Project, models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=20, choices=CHANNEL_KINDS)
    value = models.TextField(blank=True)
    email_verified = models.BooleanField(default=False)
    disabled = models.BooleanField(null=True)
    last_notify = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    checks = models.ManyToManyField(Check)

    def __str__(self):
        if self.name:
            return self.name
        if self.kind == "email":
            return "Email to %s" % self.email_value
        elif self.kind == "sms":
            return "SMS to %s" % self.phone_number
        elif self.kind == "slack":
            return "Slack %s" % self.slack_channel
        elif self.kind == "telegram":
            return "Telegram %s" % self.telegram_name
        elif self.kind == "zulip":
            if self.zulip_type == "stream":
                return "Zulip stream %s" % self.zulip_to
            if self.zulip_type == "private":
                return "Zulip user %s" % self.zulip_to

        return self.get_kind_display()

    def to_dict(self):
        return {"id": str(self.code), "name": self.name, "kind": self.kind}

    def is_editable(self):
        return self.kind in ("email", "webhook", "sms", "signal", "whatsapp")

    def assign_all_checks(self):
        checks = Check.objects.filter(project=self.project)
        self.checks.add(*checks)

    def make_token(self):
        seed = "%s%s" % (self.code, settings.SECRET_KEY)
        seed = seed.encode()
        return hashlib.sha1(seed).hexdigest()

    def send_verify_link(self):
        args = [self.code, self.make_token()]
        verify_link = reverse("hc-verify-email", args=args)
        verify_link = settings.SITE_ROOT + verify_link
        emails.verify_email(self.email_value, {"verify_link": verify_link})

    def get_unsub_link(self):
        signer = TimestampSigner(salt="alerts")
        signed_token = signer.sign(self.make_token())
        args = [self.code, signed_token]
        verify_link = reverse("hc-unsubscribe-alerts", args=args)
        return settings.SITE_ROOT + verify_link

    @property
    def transport(self):
        if self.kind == "email":
            return transports.Email(self)
        elif self.kind == "webhook":
            return transports.Webhook(self)
        elif self.kind in ("slack", "mattermost"):
            return transports.Slack(self)
        elif self.kind == "hipchat":
            return transports.HipChat(self)
        elif self.kind == "pd":
            return transports.PagerDuty(self)
        elif self.kind == "pagertree":
            return transports.PagerTree(self)
        elif self.kind == "pagerteam":
            return transports.PagerTeam(self)
        elif self.kind == "victorops":
            return transports.VictorOps(self)
        elif self.kind == "pushbullet":
            return transports.Pushbullet(self)
        elif self.kind == "po":
            return transports.Pushover(self)
        elif self.kind == "opsgenie":
            return transports.Opsgenie(self)
        elif self.kind == "discord":
            return transports.Discord(self)
        elif self.kind == "telegram":
            return transports.Telegram(self)
        elif self.kind == "sms":
            return transports.Sms(self)
        elif self.kind == "trello":
            return transports.Trello(self)
        elif self.kind == "matrix":
            return transports.Matrix(self)
        elif self.kind == "whatsapp":
            return transports.WhatsApp(self)
        elif self.kind == "apprise":
            return transports.Apprise(self)
        elif self.kind == "msteams":
            return transports.MsTeams(self)
        elif self.kind == "shell":
            return transports.Shell(self)
        elif self.kind == "zulip":
            return transports.Zulip(self)
        elif self.kind == "spike":
            return transports.Spike(self)
        elif self.kind == "call":
            return transports.Call(self)
        elif self.kind == "linenotify":
            return transports.LineNotify(self)
        elif self.kind == "signal":
            return transports.Signal(self)
        else:
            raise NotImplementedError("Unknown channel kind: %s" % self.kind)

    def notify(self, check, is_test=False):
        if self.transport.is_noop(check):
            return "no-op"

        n = Notification(channel=self)
        if is_test:
            # When sending a test notification we leave the owner field null.
            # (the passed check is a dummy, unsaved Check instance)
            pass
        else:
            n.owner = check

        n.check_status = check.status
        n.error = "Sending"
        n.save()

        error, disabled = "", self.disabled
        try:
            self.transport.notify(check, notification=n)
        except transports.TransportError as e:
            disabled = True if e.permanent else disabled
            error = e.message

        Notification.objects.filter(id=n.id).update(error=error)
        Channel.objects.filter(id=self.id).update(
            last_notify=now(), last_error=error, disabled=disabled
        )

        return error

    def icon_path(self):
        return "img/integrations/%s.png" % self.kind

    @property
    def json(self):
        return json.loads(self.value)

    @property
    def po_priority(self):
        assert self.kind == "po"
        parts = self.value.split("|")
        prio = int(parts[1])
        return PO_PRIORITIES[prio]

    def webhook_spec(self, status):
        assert self.kind == "webhook"

        doc = json.loads(self.value)
        if status == "down" and "method_down" in doc:
            return {
                "method": doc["method_down"],
                "url": doc["url_down"],
                "body": doc["body_down"],
                "headers": doc["headers_down"],
            }
        elif status == "up" and "method_up" in doc:
            return {
                "method": doc["method_up"],
                "url": doc["url_up"],
                "body": doc["body_up"],
                "headers": doc["headers_up"],
            }

    @property
    def down_webhook_spec(self):
        return self.webhook_spec("down")

    @property
    def up_webhook_spec(self):
        return self.webhook_spec("up")

    @property
    def url_down(self):
        return self.down_webhook_spec["url"]

    @property
    def url_up(self):
        return self.up_webhook_spec["url"]

    @property
    def cmd_down(self):
        assert self.kind == "shell"
        return self.json["cmd_down"]

    @property
    def cmd_up(self):
        assert self.kind == "shell"
        return self.json["cmd_up"]

    @property
    def slack_team(self):
        assert self.kind == "slack"
        if not self.value.startswith("{"):
            return None

        doc = json.loads(self.value)
        if "team_name" in doc:
            return doc["team_name"]

        if "team" in doc:
            return doc["team"]["name"]

    @property
    def slack_channel(self):
        assert self.kind == "slack"
        if not self.value.startswith("{"):
            return None

        doc = json.loads(self.value)
        return doc["incoming_webhook"]["channel"]

    @property
    def slack_webhook_url(self):
        assert self.kind in ("slack", "mattermost")
        if not self.value.startswith("{"):
            return self.value

        doc = json.loads(self.value)
        return doc["incoming_webhook"]["url"]

    @property
    def discord_webhook_url(self):
        assert self.kind == "discord"
        url = self.json["webhook"]["url"]

        # Discord migrated to discord.com,
        # and is dropping support for discordapp.com on 7 November 2020
        if url.startswith("https://discordapp.com/"):
            url = "https://discord.com/" + url[23:]

        return url

    @property
    def telegram_id(self):
        assert self.kind == "telegram"
        return self.json.get("id")

    @property
    def telegram_type(self):
        assert self.kind == "telegram"
        return self.json.get("type")

    @property
    def telegram_name(self):
        assert self.kind == "telegram"
        return self.json.get("name")

    def update_telegram_id(self, new_chat_id) -> None:
        doc = self.json
        doc["id"] = new_chat_id
        self.value = json.dumps(doc)
        self.save()

    @property
    def pd_service_key(self):
        assert self.kind == "pd"
        if not self.value.startswith("{"):
            return self.value

        return self.json["service_key"]

    @property
    def pd_account(self):
        assert self.kind == "pd"
        if self.value.startswith("{"):
            return self.json.get("account")

    @property
    def phone_number(self):
        assert self.kind in ("call", "sms", "whatsapp", "signal")
        if self.value.startswith("{"):
            return self.json["value"]

        return self.value

    @property
    def trello_token(self):
        assert self.kind == "trello"
        return self.json["token"]

    @property
    def trello_board_list(self):
        assert self.kind == "trello"
        doc = json.loads(self.value)
        return doc["board_name"], doc["list_name"]

    @property
    def trello_list_id(self):
        assert self.kind == "trello"
        return self.json["list_id"]

    @property
    def email_value(self):
        assert self.kind == "email"
        if not self.value.startswith("{"):
            return self.value

        return self.json["value"]

    @property
    def email_notify_up(self):
        assert self.kind == "email"
        if not self.value.startswith("{"):
            return True

        return self.json.get("up")

    @property
    def email_notify_down(self):
        assert self.kind == "email"
        if not self.value.startswith("{"):
            return True

        return self.json.get("down")

    @property
    def whatsapp_notify_up(self):
        assert self.kind == "whatsapp"
        return self.json["up"]

    @property
    def whatsapp_notify_down(self):
        assert self.kind == "whatsapp"
        return self.json["down"]

    @property
    def signal_notify_up(self):
        assert self.kind == "signal"
        return self.json["up"]

    @property
    def signal_notify_down(self):
        assert self.kind == "signal"
        return self.json["down"]

    @property
    def sms_notify_up(self):
        assert self.kind == "sms"
        return self.json.get("up", False)

    @property
    def sms_notify_down(self):
        assert self.kind == "sms"
        return self.json.get("down", True)

    @property
    def opsgenie_key(self):
        assert self.kind == "opsgenie"
        if not self.value.startswith("{"):
            return self.value

        return self.json["key"]

    @property
    def opsgenie_region(self):
        assert self.kind == "opsgenie"
        if not self.value.startswith("{"):
            return "us"

        return self.json["region"]

    @property
    def zulip_bot_email(self):
        assert self.kind == "zulip"
        return self.json["bot_email"]

    @property
    def zulip_site(self):
        assert self.kind == "zulip"
        doc = json.loads(self.value)
        if "site" in doc:
            return doc["site"]

        # Fallback if we don't have the site value:
        # derive it from bot's email
        _, domain = doc["bot_email"].split("@")
        return "https://" + domain

    @property
    def zulip_api_key(self):
        assert self.kind == "zulip"
        return self.json["api_key"]

    @property
    def zulip_type(self):
        assert self.kind == "zulip"
        return self.json["mtype"]

    @property
    def zulip_to(self):
        assert self.kind == "zulip"
        return self.json["to"]

    @property
    def linenotify_token(self):
        assert self.kind == "linenotify"
        return self.value


class Notification(models.Model):
    code = models.UUIDField(default=uuid.uuid4, null=True, editable=False)
    owner = models.ForeignKey(Check, models.CASCADE, null=True)
    check_status = models.CharField(max_length=6)
    channel = models.ForeignKey(Channel, models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        get_latest_by = "created"

    def status_url(self):
        path = reverse("hc-api-notification-status", args=[self.code])
        return settings.SITE_ROOT + path


class Flip(models.Model):
    owner = models.ForeignKey(Check, models.CASCADE)
    created = models.DateTimeField()
    processed = models.DateTimeField(null=True, blank=True)
    old_status = models.CharField(max_length=8, choices=STATUSES)
    new_status = models.CharField(max_length=8, choices=STATUSES)

    class Meta:
        indexes = [
            # For quickly looking up unprocessed flips.
            # Used in the sendalerts management command.
            models.Index(
                fields=["processed"],
                name="api_flip_not_processed",
                condition=models.Q(processed=None),
            )
        ]

    def to_dict(self):
        return {
            "timestamp": isostring(self.created),
            "up": 1 if self.new_status == "up" else 0,
        }

    def send_alerts(self):
        """Loop over the enabled channels, call notify() on each.

        For each channel, yield a (channel, error, send_time) triple:
         * channel is a Channel instance
         * error is an empty string ("") on success, error message otherwise
         * send_time is the send time in seconds (float)
        """

        # Don't send alerts on new->up and paused->up transitions
        if self.new_status == "up" and self.old_status in ("new", "paused"):
            return

        if self.new_status not in ("up", "down"):
            raise NotImplementedError("Unexpected status: %s" % self.status)

        for channel in self.owner.channel_set.exclude(disabled=True):
            start = time.time()
            error = channel.notify(self.owner)
            if error == "no-op":
                continue

            yield (channel, error, time.time() - start)


class TokenBucket(models.Model):
    value = models.CharField(max_length=80, unique=True)
    tokens = models.FloatField(default=1.0)
    updated = models.DateTimeField(default=now)

    @staticmethod
    def authorize(value, capacity, refill_time_secs):
        frozen_now = now()
        obj, created = TokenBucket.objects.get_or_create(value=value)

        if not created:
            # Top up the bucket:
            delta_secs = (frozen_now - obj.updated).total_seconds()
            obj.tokens = min(1.0, obj.tokens + delta_secs / refill_time_secs)

        obj.tokens -= 1.0 / capacity
        if obj.tokens < 0:
            # Not enough tokens
            return False

        # Race condition: two concurrent authorize calls can overwrite each
        # other's changes. It's OK to be a little inexact here for the sake
        # of simplicity.
        obj.updated = frozen_now
        obj.save()

        return True

    @staticmethod
    def authorize_login_email(email):
        # remove dots and alias:
        mailbox, domain = email.split("@")
        mailbox = mailbox.replace(".", "")
        mailbox = mailbox.split("+")[0]
        email = mailbox + "@" + domain

        salted_encoded = (email + settings.SECRET_KEY).encode()
        value = "em-%s" % hashlib.sha1(salted_encoded).hexdigest()

        # 20 login attempts for a single email per hour:
        return TokenBucket.authorize(value, 20, 3600)

    @staticmethod
    def authorize_invite(user):
        value = "invite-%d" % user.id

        # 20 invites per day
        return TokenBucket.authorize(value, 20, 3600 * 24)

    @staticmethod
    def authorize_login_password(email):
        salted_encoded = (email + settings.SECRET_KEY).encode()
        value = "pw-%s" % hashlib.sha1(salted_encoded).hexdigest()

        # 20 password attempts per day
        return TokenBucket.authorize(value, 20, 3600 * 24)

    @staticmethod
    def authorize_telegram(telegram_id):
        value = "tg-%s" % telegram_id

        # 6 messages for a single chat per minute:
        return TokenBucket.authorize(value, 6, 60)

    @staticmethod
    def authorize_signal(phone):
        salted_encoded = (phone + settings.SECRET_KEY).encode()
        value = "signal-%s" % hashlib.sha1(salted_encoded).hexdigest()

        # 6 messages for a single recipient per minute:
        return TokenBucket.authorize(value, 6, 60)

    @staticmethod
    def authorize_pushover(user_key):
        salted_encoded = (user_key + settings.SECRET_KEY).encode()
        value = "po-%s" % hashlib.sha1(salted_encoded).hexdigest()
        # 6 messages for a single user key per minute:
        return TokenBucket.authorize(value, 6, 60)

    @staticmethod
    def authorize_sudo_code(user):
        value = "sudo-%d" % user.id

        # 10 sudo attempts per day
        return TokenBucket.authorize(value, 10, 3600 * 24)

    @staticmethod
    def authorize_totp_attempt(user):
        value = "totp-%d" % user.id

        # 96 attempts per user per 24 hours
        # (or, on average, one attempt per 15 minutes)
        return TokenBucket.authorize(value, 96, 3600 * 24)

    @staticmethod
    def authorize_totp_code(user, code):
        value = "totpc-%d-%s" % (user.id, code)

        # A code has a validity period of 3 * 30 = 90 seconds.
        # During that period, allow the code to only be used once,
        # so an eavesdropping attacker cannot reuse a code.
        return TokenBucket.authorize(value, 1, 90)
