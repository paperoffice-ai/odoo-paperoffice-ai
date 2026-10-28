import base64
import json
import logging
import re
import threading
from datetime import datetime, timedelta

import psycopg2
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from .paperoffice_client import PaperOfficeClient, PaperOfficeNotReached, PaperOfficeResultUnknown, extract_fields

_logger = logging.getLogger(__name__)

SUPPORTED_MIMETYPES = ("application/pdf", "image/png", "image/jpeg", "image/tiff", "image/webp")
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d")
AMOUNT_TOLERANCE = 0.02
TAX_RATE_TOLERANCE = 0.05
MAX_BATCH = 20
SENDING_STALE_MINUTES = 15
QUEUED_MAX_HOURS = 24


def parse_date(value):
    if not value:
        return False
    text = str(value).strip()[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return False


def parse_amount(value):
    """Parse '1699.32 EUR', '1.699,32', '1,699.32' into a float (or None)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9,.\-]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _sep, tail = text.rpartition(",")
        text = head.replace(",", "") + "." + tail if len(tail) != 3 else text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_iban(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


class AccountMove(models.Model):
    _inherit = "account.move"

    paperoffice_state = fields.Selection(
        [("sending", "Sending"), ("queued", "Processing"), ("done", "Extracted"), ("error", "Failed")],
        string="PaperOffice AI",
        copy=False,
        readonly=True,
    )
    paperoffice_job_id = fields.Char(string="PaperOffice AI job", copy=False, readonly=True)
    paperoffice_sent_at = fields.Datetime(string="Sent to PaperOffice AI", copy=False, readonly=True)
    paperoffice_result = fields.Text(string="PaperOffice AI result", copy=False, readonly=True)
    paperoffice_warnings = fields.Text(string="PaperOffice AI checks", copy=False, readonly=True)
    paperoffice_enabled = fields.Boolean(string="PaperOffice AI available", compute="_compute_paperoffice_enabled")

    def _compute_paperoffice_enabled(self):
        enabled = self.env["ir.config_parameter"].sudo().get_param("paperoffice_ai.enabled")
        for move in self:
            move.paperoffice_enabled = bool(enabled) and move.move_type in ("in_invoice", "in_refund")

    def _paperoffice_commit(self):
        """Persist the send state right away, so a later error never hides a charged job."""
        if not getattr(threading.current_thread(), "testing", False):
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------
    def _paperoffice_get_attachment(self):
        self.ensure_one()
        attachment = self.message_main_attachment_id
        if not attachment or attachment.mimetype not in SUPPORTED_MIMETYPES:
            attachment = self.env["ir.attachment"].search(
                [("res_model", "=", "account.move"), ("res_id", "=", self.id),
                 ("mimetype", "in", SUPPORTED_MIMETYPES)],
                order="id desc", limit=1,
            )
        return attachment

    def _paperoffice_check_write_access(self):
        if hasattr(self, "check_access"):
            self.check_access("write")
        else:
            self.check_access_rights("write")
            self.check_access_rule("write")

    def _paperoffice_lock(self):
        """Row lock against double clicks and parallel sends of the same bills."""
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    "SELECT id FROM account_move WHERE id IN %s FOR UPDATE NOWAIT", [tuple(self.ids)]
                )
        except psycopg2.OperationalError:
            raise UserError(_("These bills are being sent to PaperOffice AI right now. Please wait a moment."))
        self.invalidate_recordset(["paperoffice_state", "paperoffice_job_id"])

    def action_paperoffice_extract(self):
        """Send bills that were never sent. Bills already sent need action_paperoffice_resend."""
        return self._paperoffice_send(resend=False)

    def action_paperoffice_resend(self):
        """Send again after an explicit confirmation in the view."""
        return self._paperoffice_send(resend=True)

    def _paperoffice_send(self, resend):
        if not self.env["ir.config_parameter"].sudo().get_param("paperoffice_ai.enabled"):
            raise UserError(_("PaperOffice AI is switched off. An administrator can switch it on in the Accounting settings."))
        if len(self) > MAX_BATCH:
            raise UserError(_("Please select at most %s bills at once.", MAX_BATCH))
        # 1. Check everything before anything is sent.
        self._paperoffice_check_write_access()
        self._paperoffice_lock()
        todo = []
        for move in self:
            if move.move_type not in ("in_invoice", "in_refund") or move.state != "draft":
                raise UserError(_("Only draft vendor bills and refunds can be read with PaperOffice AI."))
            if move.paperoffice_state in ("sending", "queued"):
                continue
            if move.paperoffice_state and not resend:
                continue
            attachment = move._paperoffice_get_attachment()
            if not attachment:
                raise UserError(_("Attach the supplier invoice (PDF or image) to %s first.", move.display_name))
            todo.append((move, attachment))
        if not todo:
            return True
        client = PaperOfficeClient(self.env)
        # 2. Claim all bills in ONE commit; a parallel selection then skips them.
        claimed = self._paperoffice_claim([move.id for move, _att in todo], resend)
        todo = [(move, att) for move, att in todo if move.id in claimed]
        # 3. Send one by one; each result is committed right away.
        for index, (move, attachment) in enumerate(todo):
            try:
                job_id = client.submit_invoice(attachment.name, base64.b64decode(attachment.datas), attachment.mimetype)
            except PaperOfficeResultUnknown as err:
                move.write({"paperoffice_state": "error", "paperoffice_warnings": str(err)})
                move.message_post(body=html_escape(str(err)))
                move._paperoffice_commit()
                continue
            except UserError as err:
                # Nothing was accepted for this bill: release it and all bills not sent yet.
                rest = self.browse([m.id for m, _att in todo[index:]])
                rest.write({"paperoffice_state": False, "paperoffice_sent_at": False})
                rest._paperoffice_commit()
                if index and isinstance(err, PaperOfficeNotReached):
                    raise UserError(_("PaperOffice AI could not be reached. The remaining bills were not sent. Please try again later."))
                raise
            move.write({"paperoffice_job_id": job_id, "paperoffice_state": "queued"})
            move.message_post(body=_("Sent %s to PaperOffice AI for extraction.", attachment.name))
            move._paperoffice_commit()
        return True

    def _paperoffice_claim(self, move_ids, resend):
        """Atomically set the bills to 'sending' and return the ids that were really claimed."""
        allowed = "paperoffice_state IS NULL"
        if resend:
            allowed += " OR paperoffice_state IN ('done', 'error')"
        self.flush_model()
        self.env.cr.execute(
            "UPDATE account_move SET paperoffice_state = 'sending', paperoffice_job_id = NULL, "
            "paperoffice_warnings = NULL, paperoffice_sent_at = (now() at time zone 'UTC') "
            "WHERE id IN %s AND (" + allowed + ") RETURNING id",
            [tuple(move_ids)],
        )
        claimed = {row[0] for row in self.env.cr.fetchall()}
        self.browse(move_ids).invalidate_recordset(
            ["paperoffice_state", "paperoffice_job_id", "paperoffice_warnings", "paperoffice_sent_at"]
        )
        self._paperoffice_commit()
        return claimed

    def action_paperoffice_refresh(self):
        self._paperoffice_check_write_access()
        self._paperoffice_poll()
        return True

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------
    @api.model
    def _cron_paperoffice_poll(self):
        stale = fields.Datetime.now() - timedelta(minutes=SENDING_STALE_MINUTES)
        for move in self.search([("paperoffice_state", "=", "sending"), ("paperoffice_sent_at", "<", stale)]):
            message = _(
                "No answer from PaperOffice AI. The document may have been received. "
                "Please check in PaperOffice AI before sending it again."
            )
            move.write({"paperoffice_state": "error", "paperoffice_warnings": message})
            move.message_post(body=message)
            move._paperoffice_commit()
        moves = self.search([("paperoffice_state", "=", "queued")], order="paperoffice_sent_at, id", limit=50)
        if moves:
            moves._paperoffice_poll()

    def _paperoffice_poll(self):
        moves = self.filtered(lambda m: m.paperoffice_state == "queued" and m.paperoffice_job_id)
        if not moves:
            return
        client = PaperOfficeClient(self.env)
        expired = fields.Datetime.now() - timedelta(hours=QUEUED_MAX_HOURS)
        for move in moves:
            data = None
            try:
                with self.env.cr.savepoint():
                    data = move._paperoffice_poll_one(client, expired)
            except Exception:
                _logger.exception("PaperOffice AI: could not fetch job of move %s", move.id)
            move._paperoffice_commit()
            if data is None:
                continue
            # The result is committed above; applying runs in its own savepoint.
            try:
                with self.env.cr.savepoint():
                    move._paperoffice_apply_result(data)
            except Exception:
                _logger.exception("PaperOffice AI: could not apply result to move %s", move.id)
                message = _("The result was saved but could not be applied to the bill. Please fill the bill manually.")
                move.write({"paperoffice_state": "error", "paperoffice_warnings": message})
                move.message_post(body=move._paperoffice_summary_html(data, [message]))
            move._paperoffice_commit()

    def _paperoffice_poll_one(self, client, expired):
        """Fetch the job. Returns the extracted fields once completed (already stored), else None."""
        self.ensure_one()
        try:
            body = client.get_job(self.paperoffice_job_id)
        except UserError as err:
            self.write({"paperoffice_state": "error", "paperoffice_warnings": str(err)})
            self.message_post(body=html_escape(str(err)))
            return None
        status = body and (body.get("job_status") or (body.get("job") or {}).get("job_status"))
        if status in ("failed", "error", "cancelled"):
            self.write({"paperoffice_state": "error"})
            self.message_post(body=_("PaperOffice AI could not read the document (%s).", body.get("code") or status))
        elif status == "completed":
            data = extract_fields(body.get("result") or body.get("job_result") or {})
            self.write({"paperoffice_result": json.dumps(data, ensure_ascii=False, default=str)})
            return data
        elif self.paperoffice_sent_at and self.paperoffice_sent_at < expired:
            # No final answer within the time limit (network errors and running jobs alike).
            self.write({"paperoffice_state": "error"})
            self.message_post(body=_("PaperOffice AI did not answer in time. Please try again."))
        return None

    def _paperoffice_apply_result(self, data):
        self.ensure_one()
        if self.state != "draft":
            self.write({"paperoffice_state": "done"})
            self.message_post(body=self._paperoffice_summary_html(data, [
                _("The bill is no longer a draft. Nothing was changed."),
            ]))
            return
        self._paperoffice_apply(data)

    # ------------------------------------------------------------------
    # Apply + checks
    # ------------------------------------------------------------------
    def _paperoffice_find_partner(self, data):
        Partner = self.env["res.partner"]
        company_domain = [("company_id", "in", (False, self.company_id.id))]
        vat = data.get("_supplier_vat_id") or data.get("_supplier_vat") or data.get("_supplier_tax_id")
        if vat:
            partner = Partner.search(company_domain + [("vat", "=ilike", escape_like(re.sub(r"\s+", "", str(vat))))], limit=1)
            if partner:
                return partner.commercial_partner_id
        name = data.get("_supplier_name")
        if name:
            partner = Partner.search(
                company_domain + [("name", "=ilike", escape_like(str(name).strip())), ("is_company", "=", True)], limit=1
            )
            if partner:
                return partner
        return Partner

    def _paperoffice_apply(self, data):
        self.ensure_one()
        warnings = []
        vals = {"paperoffice_state": "done"}
        if not data:
            self.write({"paperoffice_state": "error"})
            self.message_post(body=_("PaperOffice AI returned no invoice fields."))
            return

        if not self.partner_id:
            partner = self._paperoffice_find_partner(data)
            if partner:
                vals["partner_id"] = partner.id
            elif data.get("_supplier_name"):
                warnings.append(_("Supplier '%s' not found. Please select or create the vendor.", data["_supplier_name"]))
        if not self.ref and data.get("_invoice_number"):
            vals["ref"] = str(data["_invoice_number"])[:64]
        if not self.invoice_date:
            invoice_date = parse_date(data.get("_invoice_date"))
            if invoice_date:
                vals["invoice_date"] = invoice_date
        currency_code = data.get("_currency") or data.get("_currency_code")
        # The currency is only set on a bill without lines that still has the company currency.
        if currency_code and not self.invoice_line_ids and self.currency_id == self.company_id.currency_id:
            code = str(currency_code).strip().upper()[:3]
            currency = self.env["res.currency"].with_context(active_test=False).search([("name", "=", code)], limit=1)
            if currency and not currency.active:
                warnings.append(_("Currency %s on the document is not active in Odoo.", code))
            elif currency and currency != self.currency_id:
                vals["currency_id"] = currency.id

        if not self.invoice_line_ids:
            taxes, tax_warning = self._paperoffice_find_taxes(data)
            if tax_warning:
                warnings.append(tax_warning)
            vals["invoice_line_ids"] = self._paperoffice_line_commands(data, taxes)

        had_due_date = bool(self.invoice_date_due) and bool(self.invoice_date)
        self.write(vals)
        # Due date last: Odoo recomputes it from the invoice date unless a payment term is set.
        due_date = parse_date(data.get("_payment_due_date") or data.get("_due_date"))
        if due_date and not self.invoice_payment_term_id and not had_due_date:
            self.invoice_date_due = due_date
        warnings += self._paperoffice_checks(data)
        self.write({"paperoffice_warnings": "\n".join(warnings) or False})
        self.message_post(body=self._paperoffice_summary_html(data, warnings))

    def _paperoffice_find_taxes(self, data):
        """Return (purchase taxes for the single VAT rate on the document, warning or None)."""
        Tax = self.env["account.tax"]
        rates = set()
        for row in data.get("_vat_breakdown") or []:
            if isinstance(row, dict):
                rate = parse_amount(row.get("rate_percent"))
                if rate is not None:
                    rates.add(round(rate, 2))
        if not rates:
            net, vat = parse_amount(data.get("_net_amount")), parse_amount(data.get("_vat_amount"))
            if net and vat is not None:
                rates.add(round(vat / net * 100, 2))
        if len(rates) != 1:
            return Tax, (_("Several VAT rates on the document. Set the taxes per line.") if rates else None)
        rate = rates.pop()
        tax = Tax.search([
            ("type_tax_use", "=", "purchase"), ("amount_type", "=", "percent"),
            ("amount", ">=", rate - TAX_RATE_TOLERANCE), ("amount", "<=", rate + TAX_RATE_TOLERANCE),
            ("price_include", "=", False),
            *Tax._check_company_domain(self.company_id),
        ], order="sequence, id", limit=1)
        if not tax and rate:
            return Tax, _("No purchase tax with %s %% found. Set the taxes on the lines.", rate)
        return tax, None

    def _paperoffice_line_commands(self, data, taxes):
        tax_ids = [(6, 0, taxes.ids)] if taxes else False
        commands = []
        items = data.get("_line_items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("description") or item.get("name")
                if name and item.get("item_code"):
                    name = "[%s] %s" % (item["item_code"], name)
                quantity = parse_amount(item.get("quantity")) or 1.0
                price = parse_amount(item.get("unit_price_net") or item.get("unit_price"))
                if price is None:
                    total = parse_amount(item.get("total_price_net") or item.get("amount"))
                    price = total / quantity if total is not None and quantity else None
                if name and price is not None:
                    line = {"name": str(name)[:255], "quantity": quantity, "price_unit": price}
                    if tax_ids:
                        line["tax_ids"] = tax_ids
                    commands.append((0, 0, line))
        if not commands:
            net = parse_amount(data.get("_net_amount"))
            if net is not None:
                label = _("Invoice %s", data.get("_invoice_number") or "")
                line = {"name": label.strip(), "quantity": 1.0, "price_unit": net}
                if tax_ids:
                    line["tax_ids"] = tax_ids
                commands.append((0, 0, line))
        return commands

    def _paperoffice_checks(self, data):
        """Plausibility checks. They only warn; nothing is blocked or posted."""
        self.ensure_one()
        warnings = []
        total = parse_amount(data.get("_total_amount"))
        net = parse_amount(data.get("_net_amount"))
        vat = parse_amount(data.get("_vat_amount"))
        if total is not None and net is not None and vat is not None and abs(net + vat - total) > AMOUNT_TOLERANCE:
            warnings.append(_("Net plus VAT on the document does not match the total."))
        if total is not None and self.invoice_line_ids and abs(self.amount_total - total) > AMOUNT_TOLERANCE:
            warnings.append(_("Bill total differs from the document total. Check taxes and lines."))
        # Duplicates: Odoo itself warns once partner and vendor reference are set.
        iban = normalize_iban(data.get("_creditor_iban") or data.get("_iban"))
        if iban and self.partner_id:
            known = {normalize_iban(bank.acc_number) for bank in self.commercial_partner_id.bank_ids}
            if known and iban not in known:
                warnings.append(_("IBAN on the document is not one of the vendor's known bank accounts."))
            elif not known:
                warnings.append(_("Vendor has no bank account on file. Verify the IBAN before paying."))
        return warnings

    def _paperoffice_summary_html(self, data, warnings):
        labels = [
            ("_supplier_name", _("Supplier")),
            ("_invoice_number", _("Invoice number")),
            ("_invoice_date", _("Invoice date")),
            ("_net_amount", _("Net")),
            ("_vat_amount", _("VAT")),
            ("_total_amount", _("Total")),
            ("_creditor_iban", _("IBAN")),
        ]
        rows = "".join(
            "<li>%s: %s</li>" % (html_escape(label), html_escape(str(data[key])))
            for key, label in labels if key in data
        )
        html = "<p>%s</p><ul>%s</ul>" % (html_escape(_("PaperOffice AI extracted:")), rows)
        if warnings:
            html += "<p><b>%s</b></p><ul>%s</ul>" % (
                html_escape(_("Please check:")),
                "".join("<li>%s</li>" % html_escape(w) for w in warnings),
            )
        return Markup(html)
