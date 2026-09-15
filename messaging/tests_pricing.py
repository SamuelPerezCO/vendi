"""Tests for messaging.pricing: what Meta will charge for a template send,
and what the month has cost so far.

Two kinds of test live here, deliberately separated:

* **Resolution and rules** -- which of Meta's market rows prices a given
  recipient, and which sends are free. These run against the real card in
  messaging.meta_rates, because getting the market wrong is a billing error,
  not a preference.
* **Arithmetic and spend** -- these use MESSAGING_TEMPLATE_RATES to install a
  small synthetic card, so they keep passing when Meta publishes new numbers.

Only a handful of assertions pin an actual Meta figure; each is marked, and a
failure there means the shipped card is out of date, not that the code broke.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Client, MessageTemplate

from . import meta_rates, pricing
from .models import Conversation, Message

# A synthetic card, keyed by Meta's market names, for the tests that assert
# exact arithmetic. Prices are strings for the same reason meta_rates stores
# strings: a JSON number would arrive as a float and 0.0008 would already
# have drifted.
RATES = json.dumps(
    {
        "Colombia": {"marketing": "0.0125", "utility": "0.0022", "authentication": "0.0077"},
        "Other": {"marketing": "0.0500", "utility": "0.0100", "authentication": "0.0300"},
    }
)


def client(phone="+573000000001", country="CO", **extra):
    return Client.objects.create(first_name="Camila", phone=phone, country=country, **extra)


def template(category="marketing", **extra):
    return MessageTemplate.objects.create(
        name=extra.pop("name", "saludo_inicial"),
        body=extra.pop("body", "Hola {{1}}"),
        body_sample_values=extra.pop("body_sample_values", ["Camila"]),
        # Aceptada: only an approved plantilla can be sent at all.
        status=extra.pop("status", "aceptada"),
        category=category,
        **extra,
    )


class MarketResolutionTests(TestCase):
    """Which row of Meta's card prices a recipient.

    Meta bills by the recipient's country calling code, so this is the step
    that decides the price -- and the one with the expensive edge cases.
    """

    def test_the_stored_country_names_the_market(self):
        self.assertEqual(pricing.market_for(client(country="CO")), "Colombia")

    def test_the_phone_answers_when_the_country_field_is_blank(self):
        self.assertEqual(pricing.market_for(client(country="")), "Colombia")

    def test_a_country_with_no_rate_of_its_own_uses_its_regional_bucket(self):
        # Ecuador, Panama, Uruguay and a dozen more have no standalone row.
        for country, phone in [("EC", "+593991234567"), ("PA", "+5071234567")]:
            with self.subTest(country=country):
                self.assertEqual(
                    pricing.market_for(client(phone=phone, country=country)),
                    "Rest of Latin America",
                )

    def test_the_longest_calling_code_wins(self):
        # +507 (Panama) starts with +50, and +51 is Peru: matching a short
        # prefix first would bill Panama at Peru's rate.
        self.assertEqual(pricing.market_for_phone("+5071234567"), "Rest of Latin America")
        self.assertEqual(pricing.market_for_phone("+51987654321"), "Peru")

    def test_plus_one_is_split_by_nanp_area_code(self):
        # The expensive one: Dominican Republic, Jamaica and Puerto Rico
        # share +1 with the US and Canada but bill at Rest of Latin America.
        # Routing on "+1" alone would misprice them by roughly 3x.
        for phone, expected in [
            ("+12125551234", "North America"),      # New York
            ("+14165551234", "North America"),      # Toronto
            ("+18095551234", "Rest of Latin America"),  # Dominican Republic
            ("+18765551234", "Rest of Latin America"),  # Jamaica
            ("+17875551234", "Rest of Latin America"),  # Puerto Rico
        ]:
            with self.subTest(phone=phone):
                self.assertEqual(pricing.market_for_phone(phone), expected)

    def test_an_unplaceable_number_falls_to_metas_own_catch_all_row(self):
        # "Other" is a real row on Meta's card ("All other countries"), not
        # a stand-in for a missing one.
        self.assertEqual(pricing.market_for(client(phone="+9999999999", country="")), "Other")
        self.assertIn("Other", pricing.rates())

    def test_a_number_with_no_digits_resolves_to_nothing(self):
        self.assertEqual(pricing.market_for_phone(""), "")
        self.assertEqual(pricing.market_for_phone("no soy un teléfono"), "")


class RateCardTests(TestCase):
    """The shipped copy of Meta's card, and picking the right one by date."""

    def test_every_market_prices_the_three_template_categories(self):
        for card in meta_rates.RATE_CARDS:
            for market, row in card["rows"].items():
                with self.subTest(card=str(card["effective"]), market=market):
                    for category in pricing.CATEGORIES:
                        self.assertIn(category, row)
                        Decimal(row[category])  # parses, and is not a float

    def test_every_market_a_country_maps_to_exists_on_the_card(self):
        # A country pointing at a market with no rate row would price at
        # "Other" silently.
        markets = set(pricing.card_for()["rows"])
        for table in (
            meta_rates.MARKET_BY_ISO,
            meta_rates.MARKET_BY_CALLING_CODE,
            meta_rates.MARKET_BY_NANP_AREA,
        ):
            for key, market in table.items():
                with self.subTest(key=key):
                    self.assertIn(market, markets)

    def test_the_card_in_force_is_the_newest_one_already_effective(self):
        cards = sorted(meta_rates.RATE_CARDS, key=lambda c: c["effective"])
        first, last = cards[0], cards[-1]
        self.assertEqual(pricing.card_for(first["effective"])["effective"], first["effective"])
        self.assertEqual(
            pricing.card_for(last["effective"] - timedelta(days=1))["effective"],
            cards[-2]["effective"] if len(cards) > 1 else first["effective"],
        )
        # Meta publishes the next card early; it takes over on its own day.
        self.assertEqual(pricing.card_for(last["effective"])["effective"], last["effective"])
        self.assertEqual(
            pricing.card_for(date(2099, 1, 1))["effective"], last["effective"]
        )

    def test_colombia_is_priced_at_metas_published_rate(self):
        # Pins an actual Meta figure: if this fails, the shipped card is out
        # of date, not the code. Card effective 2026-07-01.
        self.assertEqual(
            pricing.rate_for("Colombia", "marketing", date(2026, 7, 1)), Decimal("0.0125")
        )
        self.assertEqual(
            pricing.rate_for("Colombia", "utility", date(2026, 7, 1)), Decimal("0.0008")
        )

    def test_the_plus_one_markets_really_are_priced_apart(self):
        # The reason the NANP split exists at all.
        on = date(2026, 7, 1)
        self.assertGreater(
            pricing.rate_for("Rest of Latin America", "marketing", on),
            pricing.rate_for("North America", "marketing", on) * 2,
        )


@override_settings(MESSAGING_TEMPLATE_RATES=RATES, MESSAGING_CURRENCY="USD")
class QuoteTests(TestCase):
    def test_prices_by_category_and_market(self):
        quote = pricing.quote(template("marketing"), client())
        self.assertEqual(quote.amount, Decimal("0.0125"))
        self.assertEqual(quote.market, "Colombia")
        self.assertEqual(quote.category, "marketing")
        self.assertEqual(quote.currency, "USD")

    def test_a_market_the_override_does_not_price_keeps_metas_number(self):
        # The override names Colombia and Other; Mexico keeps the card's.
        quote = pricing.quote(template("marketing"), client(phone="+5215512345678", country="MX"))
        self.assertEqual(quote.market, "Mexico")
        self.assertEqual(
            quote.amount, pricing.rate_for("Mexico", "marketing")
        )

    def test_utility_inside_the_open_window_is_free(self):
        # A real WhatsApp rule, and the reason window state reaches pricing:
        # "Utility templates sent within an open customer service window are
        # free" -- developers.facebook.com/.../whatsapp/pricing
        quote = pricing.quote(template("utility"), client(), window_open=True)
        self.assertTrue(quote.is_free)
        self.assertEqual(quote.amount, Decimal("0"))
        # The list price still travels, so the UI can say what it saved.
        self.assertEqual(quote.unit_amount, Decimal("0.0022"))
        self.assertTrue(quote.free_reason)

    def test_utility_outside_the_window_is_charged(self):
        quote = pricing.quote(template("utility"), client(), window_open=False)
        self.assertFalse(quote.is_free)
        self.assertEqual(quote.amount, Decimal("0.0022"))

    def test_marketing_is_charged_even_inside_the_window(self):
        quote = pricing.quote(template("marketing"), client(), window_open=True)
        self.assertEqual(quote.amount, Decimal("0.0125"))

    def test_an_unknown_category_is_quoted_as_marketing(self):
        # Over-quotes rather than under-quotes: marketing is the dearest
        # column in every market on Meta's card.
        quote = pricing.quote(template("promocional"), client())
        self.assertEqual(quote.category, "marketing")


class RateTableTests(TestCase):
    def test_the_env_overrides_only_the_rows_it_names(self):
        override = json.dumps({"Colombia": {"marketing": "0.99"}})
        with override_settings(MESSAGING_TEMPLATE_RATES=override):
            self.assertEqual(pricing.rate_for("Colombia", "marketing"), Decimal("0.99"))
            # Untouched category and untouched market keep Meta's numbers.
            card = pricing.card_for()["rows"]
            self.assertEqual(
                pricing.rate_for("Colombia", "utility"),
                Decimal(card["Colombia"]["utility"]),
            )
            self.assertEqual(
                pricing.rate_for("Mexico", "marketing"),
                Decimal(card["Mexico"]["marketing"]),
            )

    def test_a_malformed_override_falls_back_to_metas_card(self):
        # A typo in an env var must not take the app down -- and must not
        # quietly price everything at zero either.
        card = pricing.card_for()["rows"]
        for bad in ("{not json", json.dumps({"Colombia": {"promocional": "0.01"}})):
            with self.subTest(bad=bad), override_settings(MESSAGING_TEMPLATE_RATES=bad):
                self.assertEqual(
                    pricing.rate_for("Colombia", "marketing"),
                    Decimal(card["Colombia"]["marketing"]),
                )

    def test_an_override_cannot_be_mutated_into_the_shipped_card(self):
        # rates() must hand out a fresh table each call, or one request's
        # override would leak into the next.
        first = pricing.rates()
        first["Colombia"]["marketing"] = Decimal("99")
        self.assertNotEqual(pricing.rates()["Colombia"]["marketing"], Decimal("99"))


@override_settings(MESSAGING_TEMPLATE_RATES=RATES)
class SpendTests(TestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(contact=client())

    def bill(self, amount, when=None):
        message = Message.objects.create(
            conversation=self.conversation,
            direction=Message.OUTBOUND,
            body="Hola",
            billed_amount=Decimal(amount),
            billed_currency="USD",
            billed_category="marketing",
        )
        if when is not None:
            Message.objects.filter(pk=message.pk).update(timestamp=when)
        return message

    def test_month_to_date_adds_up_the_billed_messages(self):
        self.bill("0.0125")
        self.bill("0.0125")
        self.assertEqual(pricing.month_to_date(), Decimal("0.025"))

    def test_unbilled_messages_are_not_counted(self):
        # Free-form replies and everything inbound carry NULL, not zero.
        Message.objects.create(
            conversation=self.conversation, direction=Message.OUTBOUND, body="Claro"
        )
        self.assertEqual(pricing.month_to_date(), Decimal("0"))

    def test_last_month_is_not_counted(self):
        self.bill("5.00", when=pricing.month_start() - timedelta(days=1))
        self.bill("0.0125")
        self.assertEqual(pricing.month_to_date(), Decimal("0.0125"))

    def test_the_month_ends_on_the_bogota_clock_not_utc(self):
        # This project runs on TIME_ZONE="UTC" while every "today" it shows an
        # agent is the Bogotá wall clock (core.calendario.CALENDAR_TZ, reused
        # by core.estadisticas_volumen as REPORT_TZ). Bucketing spend in UTC
        # instead would move the last five hours of each month -- 19:00 to
        # midnight in Bogotá -- into the next month's total, and enforce the
        # ceiling against a month the sender does not recognise.
        late = datetime(2026, 8, 31, 20, 0, tzinfo=pricing.BILLING_TZ)
        self.assertEqual(late.astimezone(dt_timezone.utc).month, 9)  # UTC says September
        self.assertEqual(pricing.month_start(late).month, 8)         # billing says August

        # And it counts in that month's spend, not the next one's.
        self.bill("0.0125", when=late)
        self.assertEqual(pricing.month_to_date(late), Decimal("0.0125"))
        first_of_september = datetime(2026, 9, 1, 9, 0, tzinfo=pricing.BILLING_TZ)
        self.assertEqual(pricing.month_to_date(first_of_september), Decimal("0"))

    @override_settings(MESSAGING_MONTHLY_BUDGET="0.02")
    def test_the_budget_stops_the_send_that_would_cross_it(self):
        self.bill("0.0125")
        self.assertFalse(pricing.would_exceed_budget(Decimal("0.0075")))
        self.assertTrue(pricing.would_exceed_budget(Decimal("0.01")))

    def test_without_a_budget_nothing_is_ever_exceeded(self):
        self.bill("999")
        self.assertFalse(pricing.would_exceed_budget(Decimal("999")))
        self.assertIsNone(pricing.budget_state()["budget"])

    @override_settings(MESSAGING_MONTHLY_BUDGET="no-soy-un-numero")
    def test_a_malformed_budget_is_ignored_rather_than_blocking_sends(self):
        self.assertEqual(pricing.budget(), Decimal("0"))
        self.assertFalse(pricing.would_exceed_budget(Decimal("1")))


class InWindowBillingSwitchTests(TestCase):
    """A utility template inside the customer service window is billed as a
    *service* message -- and Meta starts charging for those on 2026-10-01.

    The July 2026 card prices Service as "n/a" in all 38 markets; the October
    2026 card prices it in all 47, at exactly each market's utility rate.
    quote() reads that off the card instead of hardcoding a date, so these
    tests pin both sides of the switch. No override_settings here on purpose:
    the point is the real card.
    """

    def setUp(self):
        self.client_row = client()
        self.utility = template(category="utility", name="pedido_en_camino")

    def test_before_the_switch_an_in_window_utility_send_is_free(self):
        quote = pricing.quote(
            self.utility, self.client_row, window_open=True, when=date(2026, 7, 1)
        )
        self.assertTrue(quote.is_free)
        self.assertEqual(quote.amount, Decimal("0"))
        # The list price still travels, so the dialog can show what was saved.
        self.assertEqual(quote.unit_amount, Decimal("0.0008"))

    def test_after_the_switch_the_same_send_costs_the_service_rate(self):
        quote = pricing.quote(
            self.utility, self.client_row, window_open=True, when=date(2026, 10, 1)
        )
        self.assertFalse(quote.is_free)
        self.assertEqual(
            quote.amount, pricing.rate_for("Colombia", "service", date(2026, 10, 1))
        )
        # Meta prices Service at each market's utility rate, so an in-window
        # send stops being a discount at all.
        self.assertEqual(quote.amount, quote.unit_amount)

    def test_the_reason_shown_to_the_agent_changes_with_it(self):
        free = pricing.quote(
            self.utility, self.client_row, window_open=True, when=date(2026, 7, 1)
        )
        charged = pricing.quote(
            self.utility, self.client_row, window_open=True, when=date(2026, 10, 1)
        )
        self.assertIn("no se cobran", free.free_reason)
        self.assertIn("se cobra", charged.free_reason)

    def test_marketing_is_unaffected_by_the_switch(self):
        marketing = template(category="marketing", name="promo_x")
        for on in (date(2026, 7, 1), date(2026, 10, 1)):
            with self.subTest(on=on):
                quote = pricing.quote(marketing, self.client_row, window_open=True, when=on)
                self.assertEqual(quote.amount, pricing.rate_for("Colombia", "marketing", on))
                self.assertFalse(quote.is_free)


class ServiceAllowanceTests(TestCase):
    """From 2026-10-01 Meta charges for service messages -- everything sent
    inside an open 24-hour window -- but gives each phone number a monthly
    allowance first. These pin both sides of that meter.

    No override_settings on the rates: the point is the real card, whose July
    row prices Service as "n/a" and whose October row prices it.
    """

    JULY = date(2026, 7, 1)
    OCTOBER = date(2026, 10, 1)

    def setUp(self):
        self.contact = client()
        self.utility = template(category="utility", name="pedido_en_camino")

    def quote_in_window(self, on, used=0):
        return pricing.quote(
            self.utility, self.contact, window_open=True, when=on, service_used=used
        )

    def test_before_october_the_allowance_does_not_apply(self):
        # Service is "n/a" on the July card: in-window sends are simply free,
        # allowance or not.
        quote = self.quote_in_window(self.JULY, used=10_000)
        self.assertTrue(quote.is_free)
        self.assertIn("no se cobran", quote.free_reason)

    def test_inside_the_allowance_the_send_is_free(self):
        quote = self.quote_in_window(self.OCTOBER, used=999)
        self.assertTrue(quote.is_free)
        self.assertIn("gratis del mes", quote.free_reason)
        # The list price still travels, so the dialog can show what it saved.
        self.assertGreater(quote.unit_amount, Decimal("0"))

    def test_past_the_allowance_the_service_rate_is_charged(self):
        quote = self.quote_in_window(self.OCTOBER, used=1000)
        self.assertFalse(quote.is_free)
        self.assertEqual(
            quote.amount, pricing.rate_for("Colombia", "service", self.OCTOBER)
        )
        self.assertIn("agotados", quote.free_reason)

    @override_settings(MESSAGING_SERVICE_FREE_ALLOWANCE="0")
    def test_an_account_without_the_allowance_pays_from_the_first(self):
        # The allowance is the weakest-corroborated part of the October
        # change, so an account that does not have it can switch it off.
        quote = self.quote_in_window(self.OCTOBER, used=0)
        self.assertFalse(quote.is_free)

    def test_an_unknown_usage_is_treated_as_spent(self):
        # Quoting a price nobody is charged is a smaller error than promising
        # free and then billing for it.
        quote = pricing.quote(
            self.utility, self.contact, window_open=True, when=self.OCTOBER
        )
        self.assertFalse(quote.is_free)

    @override_settings(MESSAGING_SERVICE_FREE_ALLOWANCE="no-soy-un-numero")
    def test_a_malformed_allowance_falls_back_to_metas_figure(self):
        self.assertEqual(pricing.service_allowance(), 1000)

    def test_a_marketing_send_never_touches_the_allowance(self):
        marketing = template(category="marketing", name="promo_x")
        quote = pricing.quote(
            marketing, self.contact, window_open=True, when=self.OCTOBER, service_used=0
        )
        self.assertFalse(quote.billed_as_service)
        self.assertFalse(quote.is_free)


class ServiceUsageCountTests(TestCase):
    """What the meter counts, and what it must not."""

    def setUp(self):
        self.conversation = Conversation.objects.create(contact=client())

    def message(self, **overrides):
        fields = {
            "conversation": self.conversation,
            "direction": Message.OUTBOUND,
            "body": "Hola",
            "billed_amount": Decimal("0"),
            "billed_currency": "USD",
            "billed_as_service": True,
        }
        fields.update(overrides)
        return Message.objects.create(**fields)

    def test_it_counts_service_rate_sends(self):
        self.message()
        self.message()
        self.assertEqual(pricing.service_used_this_month(), 2)

    def test_an_ordinary_send_is_not_counted(self):
        self.message(billed_as_service=False)
        self.assertEqual(pricing.service_used_this_month(), 0)

    def test_a_failed_send_does_not_eat_the_allowance(self):
        # Meta bills on delivery: a message that never left costs nothing and
        # consumes nothing.
        from .providers.types import MessageStatus

        self.message(status=MessageStatus.FAILED.value)
        self.assertEqual(pricing.service_used_this_month(), 0)

    def test_last_month_does_not_count(self):
        # The allowance does not roll over, and neither does its usage.
        old = self.message()
        Message.objects.filter(pk=old.pk).update(
            timestamp=pricing.month_start() - timedelta(days=1)
        )
        self.assertEqual(pricing.service_used_this_month(), 0)


class SendTemplateStampsTheMarkerTests(TestCase):
    """The counter is only right if the send path stamps the row."""

    def test_an_in_window_utility_send_is_marked(self):
        from messaging import services

        contact = client()
        conversation = Conversation.objects.create(
            contact=contact, last_inbound_at=timezone.now()
        )
        utility = template(category="utility", name="pedido_en_camino")

        message = services.send_template(conversation, utility, {"1": "#4512"})

        self.assertTrue(message.billed_as_service)
        self.assertEqual(pricing.service_used_this_month(), 1)

    def test_an_out_of_window_send_is_not(self):
        from messaging import services

        conversation = Conversation.objects.create(contact=client())
        utility = template(category="utility", name="pedido_en_camino")

        message = services.send_template(conversation, utility, {"1": "#4512"})

        self.assertFalse(message.billed_as_service)
        self.assertEqual(pricing.service_used_this_month(), 0)
