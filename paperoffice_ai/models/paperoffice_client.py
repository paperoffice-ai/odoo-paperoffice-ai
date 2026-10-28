import logging
from urllib.parse import urlparse

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.paperoffice.ai/latest"
TIMEOUT = 60


class PaperOfficeResultUnknown(UserError):
    """The request may have reached PaperOffice AI, but no usable answer came back."""


class PaperOfficeNotReached(UserError):
    """Nothing was sent (connection could not be opened)."""


def _allowed_base_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "paperoffice.ai" or host.endswith(".paperoffice.ai"))


class PaperOfficeClient:
    """Thin client for the PaperOffice AI REST API (invoice IDP only)."""

    def __init__(self, env):
        self.env = env  # lets odoo._ find the user language
        params = env["ir.config_parameter"].sudo()
        self.api_key = params.get_param("paperoffice_ai.api_key") or ""
        self.base_url = (params.get_param("paperoffice_ai.base_url") or DEFAULT_BASE_URL).rstrip("/")
        if not _allowed_base_url(self.base_url):
            _logger.warning("PaperOffice AI: ignoring base URL outside https://*.paperoffice.ai")
            self.base_url = DEFAULT_BASE_URL
        self.model = params.get_param("paperoffice_ai.model") or "basic-pro-max"
        if not self.api_key:
            raise UserError(_("Please enter your PaperOffice AI API key in the Accounting settings."))

    def _headers(self):
        return {
            "Authorization": "Bearer %s" % self.api_key,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; PaperOffice-AI-Odoo/18.0.1.2.0)",
        }

    def _check(self, response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code in (401, 403):
            raise UserError(_(
                "PaperOffice AI rejected the API key. Create a User token (po_ut_...) "
                "under Account > API and enter it in the Accounting settings."
            ))
        if response.status_code >= 400 or body.get("status") == "error":
            code = body.get("code") or response.status_code
            _logger.warning("PaperOffice AI error %s", code)
            raise UserError(_("PaperOffice AI returned an error (%s).", code))
        return body

    def submit_invoice(self, filename, content, mimetype):
        """Queue an invoice extraction job and return the job id.

        A connect timeout means nothing was sent. Any other network error after the
        upload started may have created a job, so PaperOfficeResultUnknown is raised
        and the caller must not send again on its own.
        """
        try:
            response = requests.post(
                "%s/job/add/workflow" % self.base_url,
                headers=self._headers(),
                data={"idp_collection": "invoice", "model": self.model, "async_only": "true"},
                files={"file_1": (filename, content, mimetype)},
                timeout=TIMEOUT,
            )
        except requests.exceptions.ConnectTimeout:
            raise PaperOfficeNotReached(_("PaperOffice AI could not be reached. Nothing was sent. Please try again later."))
        except requests.exceptions.RequestException as err:
            _logger.warning("PaperOffice AI submit failed: %s", type(err).__name__)
            raise self._result_unknown()
        if response.status_code >= 500:
            # A gateway error (502/504/524) may come after the job was created.
            _logger.warning("PaperOffice AI submit returned HTTP %s", response.status_code)
            raise self._result_unknown()
        body = self._check(response)
        job_id = body.get("job_id") or (body.get("job") or {}).get("job_id")
        if not job_id:
            _logger.warning("PaperOffice AI submit returned no job id")
            raise self._result_unknown()
        return job_id

    def _result_unknown(self):
        return PaperOfficeResultUnknown(_(
            "No answer from PaperOffice AI. The document may have been received. "
            "Please check in PaperOffice AI before sending it again."
        ))

    def get_job(self, job_id):
        """Fetch a job. Returns None on a network error (the next poll retries)."""
        try:
            response = requests.get(
                "%s/job/get/%s" % (self.base_url, job_id),
                headers=self._headers(),
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as err:
            _logger.info("PaperOffice AI poll failed, retrying later: %s", type(err).__name__)
            return None
        if response.status_code >= 500 or response.status_code in (401, 403, 429):
            # Temporary or key problem: the job itself is fine, ask again later.
            _logger.info("PaperOffice AI poll returned HTTP %s, retrying later", response.status_code)
            return None
        return self._check(response)


def extract_fields(payload):
    """Return a flat dict {field_key: value} from the first IDP page.

    suggested_fields comes in two shapes: a dict keyed by field or a list of
    records carrying `key`/`category`. Both are handled; VALUE_MISSING is dropped.
    """
    pages = (payload or {}).get("pages_idp") or []
    if not pages:
        return {}
    suggested = pages[0].get("suggested_fields") or {}
    items = suggested.items() if isinstance(suggested, dict) else (
        (rec.get("key") or rec.get("category"), rec) for rec in suggested if isinstance(rec, dict)
    )
    fields = {}
    for key, rec in items:
        if not key or not isinstance(rec, dict):
            continue
        value = rec.get("value_raw") if rec.get("value_raw") not in (None, "") else rec.get("value")
        if value in (None, "", "VALUE_MISSING"):
            continue
        fields[key] = value
    return fields
