{
    "name": "PaperOffice AI - Invoice Extraction",
    "summary": "Read supplier invoices with PaperOffice AI and fill vendor bills",
    "version": "18.0.1.2.0",
    "category": "Accounting/Accounting",
    "author": "PaperOffice Enterprise Operations, S.L.U.",
    "website": "https://paperoffice.ai",
    "license": "LGPL-3",
    "depends": ["account"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "data/ir_cron.xml",
        "views/res_config_settings_views.xml",
        "views/account_move_views.xml",
    ],
    "images": ["static/description/screenshot_bill.png"],
    "installable": True,
    "application": False,
}
