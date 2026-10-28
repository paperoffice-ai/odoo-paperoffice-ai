from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    paperoffice_api_key = fields.Char(
        string="PaperOffice AI API key",
        config_parameter="paperoffice_ai.api_key",
        groups="base.group_system",
    )
    paperoffice_model = fields.Selection(
        [("basic-pro-max", "Basic"), ("premium-pro-max", "Premium"), ("ultra-pro-max", "Ultra")],
        string="PaperOffice AI model",
        config_parameter="paperoffice_ai.model",
        default="basic-pro-max",
    )
    paperoffice_enabled = fields.Boolean(
        string="Send vendor bills to PaperOffice AI (paid)",
        config_parameter="paperoffice_ai.enabled",
        help="When enabled, the attached document of a vendor bill is sent to PaperOffice AI "
        "for extraction when a user clicks the button. Nothing is sent automatically."
        " No specific plan is required. Usage is billed to your PaperOffice AI account as usual.",
    )
