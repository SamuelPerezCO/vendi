"""Tests for the pricing_analytics sweep: the CRM's ledger against Meta's.

The CRM's own numbers are per-message and only as complete as the delivery
receipts it received. This sweep asks Meta what the account was charged over
a window and reports the difference. Meta's API is mocked; the payload is the
documented shape, including its awkward parts -- a nested envelope, upper-case
enums with underscores, and a `cost` key that is simply absent for accounts
billed through a solution partner.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Client
from messaging import pricing
from messaging.models import Conversation, Message
from messaging.providers.meta import MetaProvider


def point(category="MARKETING", cost=10.0, volume=1, pricing_type="REGULAR", **extra):
    data_point = {
        "start": 1748761200,
        "end": 1748847600,
        "pricing_type": pricing_type,
        "pricing_category": category,
        "volume": volume,
    }
    if cost is not None:
        data_point["cost"] = cost
    data_point.update(extra)
    return data_point


class CanonicalCategoryTests(TestCase):
    def test_metas_three_spellings_collapse_to_one(self):
        # Webhook: authentication-international. Analytics:
        # AUTHENTICATION_INTERNATIONAL. Same rate bucket.
        for spelling in (
            "authentication-international",
            "AUTHENTICATION_INTERNATIONAL",
            "Authentication_International",
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    pricing.canonical_category(spelling), "authentication-international"
                )

    def test_it_survives_nothing(self):
        self.assertEqual(pricing.canonical_category(""), "")
        self.assertEqual(pricing.canonical_category(None), "")


class MetaSpendByCategoryTests(TestCase):
    def test_costs_add_up_per_category(self):
        totals = pricing.meta_spend_by_category([
            point("MARKETING", 10.0), point("MARKETING", 40.0), point("UTILITY", 1.5),
        ])
        self.assertEqual(totals["marketing"], Decimal("50.0"))
        self.assertEqual(totals["utility"], Decimal("1.5"))

    def test_a_point_without_cost_is_unknown_not_zero(self):
        # Meta omits cost entirely for partner-billed accounts. Counting it
        # as zero would understate the invoice by its whole value.
        totals = pricing.meta_spend_by_category([
            point("MARKETING", cost=None, volume=7), point("MARKETING", 10.0),
        ])
        self.assertEqual(totals["marketing"], Decimal("10.0"))
        self.assertEqual(totals["_meta"]["points_without_cost"], 1)
        self.assertEqual(totals["_meta"]["volume"], 8)

    def test_free_points_still_count_toward_volume(self):
        totals = pricing.meta_spend_by_category([
            point("SERVICE", 0, volume=6, pricing_type="FREE_ENTRY_POINT"),
        ])
        self.assertEqual(totals["service"], Decimal("0"))
        self.assertEqual(totals["_meta"]["volume"], 6)

    def test_no_points_is_not_an_error(self):
        totals = pricing.meta_spend_by_category([])
        self.assertEqual(totals["_meta"], {"points_without_cost": 0, "volume": 0})


class CrmSpendByCategoryTests(TestCase):
    def setUp(self):
        contact = Client.objects.create(first_name="Camila", phone="+573000000001")
        self.conversation = Conversation.objects.create(contact=contact)

    def bill(self, amount, category="marketing"):
        return Message.objects.create(
            conversation=self.conversation,
            direction=Message.OUTBOUND,
            body="Hola",
            billed_amount=Decimal(amount),
            billed_category=category,
            billed_currency="USD",
        )

    def test_it_totals_the_ledger_per_category(self):
        self.bill("0.0125")
        self.bill("0.0125")
        self.bill("0.0008", category="utility")
        totals = pricing.crm_spend_by_category(pricing.month_start())
        self.assertEqual(totals["marketing"], Decimal("0.025"))
        self.assertEqual(totals["utility"], Decimal("0.0008"))

    def test_unbilled_messages_are_excluded(self):
        Message.objects.create(
            conversation=self.conversation, direction=Message.OUTBOUND, body="Claro"
        )
        self.assertEqual(pricing.crm_spend_by_category(pricing.month_start()), {})

    def test_the_window_is_respected(self):
        old = self.bill("9.99")
        Message.objects.filter(pk=old.pk).update(
            timestamp=pricing.month_start() - timedelta(days=1)
        )
        self.bill("0.0125")
        totals = pricing.crm_spend_by_category(pricing.month_start())
        self.assertEqual(totals["marketing"], Decimal("0.0125"))


ENVELOPE = {
    "pricing_analytics": {
        "data": [{"data_points": [point("MARKETING", 10.0), point("UTILITY", 1.0)]}]
    }
}


@override_settings(META_ACCESS_TOKEN="tok", META_WABA_ID="123")
class FetchPricingAnalyticsTests(TestCase):
    def test_it_reads_metas_nested_envelope(self):
        response = Mock()
        response.json.return_value = ENVELOPE
        response.raise_for_status = lambda: None
        with patch("messaging.providers.meta.requests.get", return_value=response):
            points = MetaProvider().fetch_pricing_analytics(
                timezone.now() - timedelta(days=30), timezone.now()
            )
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["pricing_category"], "MARKETING")

    def test_the_parameters_travel_inside_the_field_expression(self):
        # pricing_analytics is a field expression, not a normal edge: start,
        # end and granularity go inside the field name.
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["url"], captured["params"] = url, params
            response = Mock()
            response.json.return_value = ENVELOPE
            response.raise_for_status = lambda: None
            return response

        start = timezone.make_aware(datetime(2026, 8, 1))
        end = timezone.make_aware(datetime(2026, 9, 1))
        with patch("messaging.providers.meta.requests.get", side_effect=fake_get):
            MetaProvider().fetch_pricing_analytics(start, end, granularity="DAILY")

        field = captured["params"]["fields"]
        self.assertTrue(field.startswith("pricing_analytics.start("))
        self.assertIn(f".start({int(start.timestamp())})", field)
        self.assertIn(f".end({int(end.timestamp())})", field)
        self.assertIn(".granularity(DAILY)", field)
        self.assertIn(".metric_types(COST,VOLUME)", field)
        self.assertTrue(captured["url"].endswith("/123"))

    def test_it_refuses_to_run_unconfigured(self):
        with override_settings(META_WABA_ID=""):
            with self.assertRaises(RuntimeError):
                MetaProvider().fetch_pricing_analytics(
                    timezone.now() - timedelta(days=1), timezone.now()
                )


class MetaSpendCommandTests(TestCase):
    def run_command(self, points, account=None, **options):
        provider = Mock()
        provider.name = "meta"
        provider.fetch_pricing_analytics.return_value = points
        provider.fetch_account.return_value = account or {
            "id": "123", "currency": "USD", "is_shared_with_partners": False,
        }
        out = StringIO()
        with patch(
            "messaging.management.commands.meta_spend.get_provider",
            return_value=provider,
        ):
            call_command("meta_spend", stdout=out, **options)
        return out.getvalue()

    def test_it_reports_meta_and_the_crm_side_by_side(self):
        output = self.run_command([point("MARKETING", 10.0)])
        self.assertIn("marketing", output)
        self.assertIn("10.0", output)
        self.assertIn("total", output)

    def test_a_partner_billed_account_is_called_out_not_reported_as_zero(self):
        output = self.run_command(
            [point("MARKETING", cost=None, volume=5)],
            account={"id": "123", "currency": "USD", "is_shared_with_partners": True},
        )
        self.assertIn("solution partner", output)
        self.assertIn("5 mensajes entregados", output)

    def test_it_says_metas_numbers_are_approximate(self):
        self.assertIn("factura manda", self.run_command([point()]))

    def test_an_empty_window_says_so(self):
        self.assertIn("Nothing billed", self.run_command([]))

    def test_a_bad_month_is_rejected(self):
        with self.assertRaises(CommandError):
            self.run_command([point()], month="agosto")

    def test_a_provider_without_analytics_says_so(self):
        # The stub provider tests run on has no billing analytics at all.
        with self.assertRaises(CommandError):
            call_command("meta_spend", stdout=StringIO())
