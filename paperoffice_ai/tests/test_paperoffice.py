import base64
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import mute_logger
from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.account_move import escape_like, normalize_iban, parse_amount, parse_date
from ..models.paperoffice_client import PaperOfficeClient, extract_fields

POST = "odoo.addons.paperoffice_ai.models.paperoffice_client.requests.post"
GET = "odoo.addons.paperoffice_ai.models.paperoffice_client.requests.get"

COMPLETED = {
    "status": "success",
    "job_status": "completed",
    "result": {"pages_idp": [{"suggested_fields": {
        "_supplier_name": {"value": "Azure Interior"},
        "_invoice_number": {"value": "INV-2026-0042"},
        "_invoice_date": {"value": "2026-10-01", "value_raw": "2026-10-01"},
        "_payment_due_date": {"value": "2026-10-31"},
        "_currency": {"value": "EUR"},
        "_net_amount": {"value": 100.0},
        "_vat_amount": {"value": 19.0},
        "_total_amount": {"value": 119.0},
        "_line_items": {"value": [
            {"row": 1, "item_code": "A-1", "description": "Desk", "quantity": "2", "unit_price_net": "€ 40,00"},
            {"row": 2, "description": "Chair", "quantity": "1", "total_price_net": "€ 20,00"},
        ]},
        "_vat_breakdown": {"value": [{"row": 1, "rate_percent": 19, "taxable_base_net": 100.0, "vat_amount": 19.0}]},
        "_creditor_iban": {"value": "DE89 3704 0044 0532 0130 00"},
        "_customer_name": {"value": "VALUE_MISSING"},
    }}]},
}


def _response(status, body):
    response = MagicMock(status_code=status)
    response.json.return_value = body
    return response


def _accepted(job_id="job-1"):
    return _response(200, {"status": "success", "job_id": job_id})


@tagged("post_install", "-at_install")
class TestPaperOffice(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("paperoffice_ai.api_key", "po_ut_test")
        params.set_param("paperoffice_ai.enabled", "True")
        cls.env.ref("base.EUR").active = True
        cls.tax19 = cls.env["account.tax"].create({
            "name": "Test 19 % purchase", "amount": 19, "amount_type": "percent", "type_tax_use": "purchase",
            "company_id": cls.env.company.id,
        })
        cls.vendor = cls.env["res.partner"].create({"name": "Azure Interior", "is_company": True})
        cls.bill = cls._make_bill()

    @classmethod
    def _make_bill(cls, attach=True):
        bill = cls.env["account.move"].create({"move_type": "in_invoice"})
        if attach:
            cls.env["ir.attachment"].create({
                "name": "invoice.pdf", "res_model": "account.move", "res_id": bill.id,
                "datas": base64.b64encode(b"%PDF-1.4 test"), "mimetype": "application/pdf",
            })
        return bill

    def _extract_done(self, bill):
        with patch(POST, return_value=_accepted()), patch(GET, return_value=_response(200, COMPLETED)):
            bill.action_paperoffice_extract()
            bill.action_paperoffice_refresh()

    def test_helpers(self):
        self.assertEqual(parse_amount("1.699,32 EUR"), 1699.32)
        self.assertEqual(parse_amount("1,699.32"), 1699.32)
        self.assertEqual(parse_amount("1699.32 EUR"), 1699.32)
        self.assertEqual(parse_amount("1,000"), 1000.0)
        self.assertEqual(parse_amount("1,699"), 1699.0)
        self.assertEqual(str(parse_date("01.10.2026")), "2026-10-01")
        self.assertEqual(normalize_iban("de89 3704"), "DE893704")
        self.assertEqual(escape_like("A_B%"), "A\\_B\\%")
        fields_list = extract_fields({"pages_idp": [{"suggested_fields": [
            {"key": "_supplier_name", "value": "A"}, {"category": "_total_amount", "value": "VALUE_MISSING"}]}]})
        self.assertEqual(fields_list, {"_supplier_name": "A"})

    def test_base_url_guard(self):
        self.env["ir.config_parameter"].sudo().set_param("paperoffice_ai.base_url", "http://example.com")
        self.assertEqual(PaperOfficeClient(self.env).base_url, "https://api.paperoffice.ai/latest")

    def test_submit_poll_apply(self):
        with patch(POST, return_value=_accepted()) as post:
            self.bill.action_paperoffice_extract()
            data = post.call_args.kwargs["data"]
            self.assertEqual(data["idp_collection"], "invoice")
            self.assertEqual(data["async_only"], "true")
        self.assertEqual(self.bill.paperoffice_state, "queued")
        self.assertTrue(self.bill.paperoffice_sent_at)

        with patch(GET, return_value=_response(200, COMPLETED)):
            self.env["account.move"]._cron_paperoffice_poll()

        bill = self.bill
        self.assertEqual(bill.paperoffice_state, "done")
        self.assertEqual(bill.partner_id, self.vendor)
        self.assertEqual(bill.ref, "INV-2026-0042")
        self.assertEqual(str(bill.invoice_date), "2026-10-01")
        self.assertEqual(str(bill.invoice_date_due), "2026-10-31")
        self.assertEqual(bill.currency_id.name, "EUR")
        lines = bill.invoice_line_ids.sorted("sequence")
        self.assertEqual(lines.mapped("name"), ["[A-1] Desk", "Chair"])
        self.assertEqual(lines.mapped("price_unit"), [40.0, 20.0])
        self.assertEqual(lines.tax_ids, self.tax19)
        self.assertAlmostEqual(bill.amount_total, 119.0)
        self.assertIn("bank account", bill.paperoffice_warnings)
        self.assertNotIn("total", bill.paperoffice_warnings)
        self.assertEqual(json.loads(bill.paperoffice_result)["_invoice_number"], "INV-2026-0042")

    def test_reclick_after_done_needs_resend(self):
        self._extract_done(self.bill)
        with patch(POST, return_value=_accepted("job-2")) as post:
            self.bill.action_paperoffice_extract()
            post.assert_not_called()
            self.bill.action_paperoffice_resend()
            post.assert_called_once()
        self.assertEqual(self.bill.paperoffice_job_id, "job-2")
        self.assertEqual(self.bill.paperoffice_state, "queued")

    def test_double_click_while_queued(self):
        with patch(POST, return_value=_accepted()) as post:
            self.bill.action_paperoffice_extract()
            self.bill.action_paperoffice_extract()
            self.bill.action_paperoffice_resend()
            post.assert_called_once()

    def test_partial_error_in_list_sends_nothing(self):
        without_attachment = self._make_bill(attach=False)
        with patch(POST, return_value=_accepted()) as post:
            with self.assertRaises(UserError):
                (self.bill | without_attachment).action_paperoffice_extract()
            post.assert_not_called()
        self.assertFalse(self.bill.paperoffice_state)

    def test_batch_limit(self):
        bills = self.env["account.move"].create([{"move_type": "in_invoice"} for _i in range(21)])
        with patch(POST) as post:
            with self.assertRaises(UserError):
                bills.action_paperoffice_extract()
            post.assert_not_called()

    def test_readonly_user_cannot_send(self):
        user = self.env["res.users"].create({
            "name": "Read only", "login": "po_readonly",
            "groups_id": [(6, 0, [self.env.ref("account.group_account_readonly").id, self.env.ref("base.group_user").id])],
            "company_ids": [(6, 0, self.env.company.ids)], "company_id": self.env.company.id,
        })
        with patch(POST, return_value=_accepted()) as post:
            with self.assertRaises(AccessError):
                self.bill.with_user(user).action_paperoffice_extract()
            post.assert_not_called()

    def test_read_timeout_marks_unknown(self):
        with patch(POST, side_effect=requests.exceptions.ReadTimeout()) as post:
            self.bill.action_paperoffice_extract()
            post.assert_called_once()
        self.assertEqual(self.bill.paperoffice_state, "error")
        self.assertIn("before sending it again", self.bill.paperoffice_warnings)
        with patch(POST, return_value=_accepted()) as post:
            self.bill.action_paperoffice_extract()
            post.assert_not_called()

    def test_connect_timeout_sends_nothing(self):
        with patch(POST, side_effect=requests.exceptions.ConnectTimeout()):
            with self.assertRaisesRegex(UserError, "Nothing was sent"):
                self.bill.action_paperoffice_extract()

    def test_poll_network_error_keeps_queued(self):
        with patch(POST, return_value=_accepted()):
            self.bill.action_paperoffice_extract()
        with patch(GET, side_effect=requests.exceptions.ConnectionError()):
            self.env["account.move"]._cron_paperoffice_poll()
        self.assertEqual(self.bill.paperoffice_state, "queued")

    def test_currency_not_overwritten_when_lines_exist(self):
        bill = self._make_bill()
        bill.write({"invoice_line_ids": [(0, 0, {"name": "Manual", "quantity": 1, "price_unit": 5})]})
        currency = bill.currency_id
        self._extract_done(bill)
        self.assertEqual(bill.currency_id, currency)
        self.assertEqual(bill.invoice_line_ids.mapped("name"), ["Manual"])

    def test_not_draft_anymore_keeps_result(self):
        with patch(POST, return_value=_accepted()):
            self.bill.action_paperoffice_extract()
        self.bill.button_cancel()
        with patch(GET, return_value=_response(200, COMPLETED)):
            self.bill.action_paperoffice_refresh()
        self.assertEqual(self.bill.paperoffice_state, "done")
        self.assertFalse(self.bill.ref)
        self.assertTrue(self.bill.paperoffice_result)

    def test_auth_error(self):
        with patch(POST, return_value=_response(401, {"status": "error", "code": "TOKEN_NOT_FOUND"})):
            with self.assertRaisesRegex(UserError, "User token"):
                self.bill.action_paperoffice_extract()
        self.assertFalse(self.bill.paperoffice_state)

    def test_disabled(self):
        self.env["ir.config_parameter"].sudo().set_param("paperoffice_ai.enabled", "")
        with self.assertRaises(UserError):
            self.bill.action_paperoffice_extract()

    def test_failed_job(self):
        self.bill.write({"paperoffice_state": "queued", "paperoffice_job_id": "job-2"})
        with patch(GET, return_value=_response(200, {"status": "success", "job_status": "failed", "code": "FILE_REQUIRED"})):
            self.bill.action_paperoffice_refresh()
        self.assertEqual(self.bill.paperoffice_state, "error")

    def test_submit_gateway_error_is_unknown(self):
        for response in (_response(502, {}), _response(200, {"status": "success"})):
            bill = self._make_bill()
            with patch(POST, return_value=response):
                bill.action_paperoffice_extract()
            self.assertEqual(bill.paperoffice_state, "error")
            self.assertIn("before sending it again", bill.paperoffice_warnings)
            with patch(POST, return_value=_accepted()) as post:
                bill.action_paperoffice_extract()
                post.assert_not_called()

    def test_claim_is_atomic(self):
        other = self._make_bill()
        other.paperoffice_state = "queued"
        claimed = self.env["account.move"]._paperoffice_claim([self.bill.id, other.id], False)
        self.assertEqual(claimed, {self.bill.id})
        self.assertEqual(self.bill.paperoffice_state, "sending")
        self.assertEqual(self.env["account.move"]._paperoffice_claim([self.bill.id], True), set())

    def test_connect_timeout_mid_list(self):
        second = self._make_bill()
        side_effect = [_accepted(), requests.exceptions.ConnectTimeout()]
        with patch(POST, side_effect=side_effect) as post:
            with self.assertRaisesRegex(UserError, "remaining bills"):
                (self.bill | second).action_paperoffice_extract()
            self.assertEqual(post.call_count, 2)

    def test_cron_stale_sending(self):
        self.bill.write({"paperoffice_state": "sending",
                         "paperoffice_sent_at": fields.Datetime.now() - timedelta(hours=1)})
        self.env["account.move"]._cron_paperoffice_poll()
        self.assertEqual(self.bill.paperoffice_state, "error")
        self.assertIn("before sending it again", self.bill.paperoffice_warnings)

    def test_queued_time_limit(self):
        self.bill.write({"paperoffice_state": "queued", "paperoffice_job_id": "job-9",
                         "paperoffice_sent_at": fields.Datetime.now() - timedelta(hours=25)})
        with patch(GET, return_value=_response(200, {"status": "success", "job_status": "processing"})):
            self.env["account.move"]._cron_paperoffice_poll()
        self.assertEqual(self.bill.paperoffice_state, "error")

    def test_poll_gateway_error_keeps_queued(self):
        self.bill.write({"paperoffice_state": "queued", "paperoffice_job_id": "job-9",
                         "paperoffice_sent_at": fields.Datetime.now()})
        for status in (502, 429, 401):
            with patch(GET, return_value=_response(status, {})):
                self.env["account.move"]._cron_paperoffice_poll()
            self.assertEqual(self.bill.paperoffice_state, "queued")

    def test_apply_failure_keeps_result(self):
        self.bill.write({"paperoffice_state": "queued", "paperoffice_job_id": "job-9",
                         "paperoffice_sent_at": fields.Datetime.now()})
        with patch(GET, return_value=_response(200, COMPLETED)), \
                patch.object(type(self.bill), "_paperoffice_apply", side_effect=ValueError("boom")), \
                mute_logger("odoo.addons.paperoffice_ai.models.account_move"):
            self.env["account.move"]._cron_paperoffice_poll()
        self.assertEqual(self.bill.paperoffice_state, "error")
        self.assertIn("saved", self.bill.paperoffice_warnings)
        self.assertEqual(json.loads(self.bill.paperoffice_result)["_invoice_number"], "INV-2026-0042")

    def test_tax_rate_with_decimals(self):
        tax77 = self.env["account.tax"].create({
            "name": "Test 7.7 % purchase", "amount": 7.7, "amount_type": "percent", "type_tax_use": "purchase",
            "company_id": self.env.company.id,
        })
        taxes, warning = self.bill._paperoffice_find_taxes({"_vat_breakdown": [{"rate_percent": 7.7}]})
        self.assertEqual(taxes, tax77)
        self.assertFalse(warning)
        taxes, warning = self.bill._paperoffice_find_taxes({"_net_amount": 100.0, "_vat_amount": 7.7})
        self.assertEqual(taxes, tax77)
