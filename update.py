"""
Regenerates skills/*/references/ files from Claris llms-full.txt.

Usage:
    python update.py                      # fetches fresh llms-full.txt from Claris
    python update.py snapshots/llms-full.txt   # uses a local snapshot instead
"""

import sys
import os
import re
import urllib.request
from pathlib import Path
from datetime import date

REPO_ROOT = Path(__file__).resolve().parent
SKILLS_DIR = REPO_ROOT / "skills"
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"

LLMS_FULL_URL = "https://help.claris.com/llms-full.txt"

# ---------------------------------------------------------------------------
# Design decisions
#
# Reference file sizing: aim for ≤80 links per file — a guideline, not a
# hard limit. Under 50 is trivially fine; 50-80 is still a single file;
# above 80, split along natural topic boundaries where a meaningful split
# exists. Over 150, always split. Rationale: each link is ~30-35 tokens;
# very large unsplit files waste context and risk picking wrong pages.
# Exception: pro-func-get.md (~140 links) is kept as one file because Get()
# functions form a single flat category with no meaningful sub-grouping.
#
# Link format: Markdown link-list (matching llms-full.txt source format).
# Compact, clickable in editors, trivial to emit.
#
# Section-to-skill mapping
#
# Keys are the ### headers from llms-full.txt (English section).
# Values are (skill_name, reference_file) tuples.
# Multiple headers can map to the same skill with different reference files.
# ---------------------------------------------------------------------------

SECTION_MAP = {
    # data-api
    "Claris FileMaker Data API Guide": ("data-api", "data-api.md"),

    # admin-api
    "Claris FileMaker Admin API Guide": ("admin-api", "admin-api.md"),

    # odata
    "Claris FileMaker OData Guide": ("odata", "odata.md"),

    # sql-reference
    "Claris FileMaker SQL Reference": ("sql-reference", "sql-reference.md"),

    # security-guide
    "Claris FileMaker Security Guide": ("security-guide", "security-guide.md"),

    # app-upgrade-tool
    "Claris FileMaker App Upgrade Tool Guide": ("app-upgrade-tool", "app-upgrade-tool.md"),

    # data-migration-tool
    "Claris FileMaker Data Migration Tool Guide": ("data-migration-tool", "data-migration-tool.md"),

    # developer-tool
    "Claris FileMaker Developer Tool Guide": ("developer-tool", "developer-tool.md"),

    # filemaker-go
    "Claris FileMaker Go Help": ("filemaker-go", "go-help.md"),
    "Claris FileMaker Go Development Guide": ("filemaker-go", "go-development.md"),
    "Claris FileMaker Go Release Notes": ("filemaker-go", "go-release-notes.md"),
    "Claris FileMaker iOS App SDK Guide": ("filemaker-go", "ios-app-sdk.md"),

    # filemaker-cloud
    "Claris FileMaker Cloud Getting Started Guide": ("filemaker-cloud", "cloud-getting-started.md"),
    "Claris FileMaker Cloud Help": ("filemaker-cloud", "cloud-administration.md"),
    "Claris FileMaker Cloud Release Notes": ("filemaker-cloud", "cloud-release-notes.md"),

    # filemaker-server
    "Claris FileMaker Server Help": ("filemaker-server", "SPLIT"),
    "Claris FileMaker Server Installation and Configuration Guide": ("filemaker-server", "server-installation.md"),
    "Claris FileMaker Server Network Install Setup Guide": ("filemaker-server", "server-network-install.md"),
    "Claris FileMaker Server Release Notes": ("filemaker-server", "server-release-notes.md"),

    # filemaker-webdirect
    "Claris FileMaker WebDirect Guide": ("filemaker-webdirect", "webdirect.md"),

    # claris-connect
    "Claris Connect Help": ("claris-connect", "connect-help.md"),
    "Claris Connect Reference": ("claris-connect", "SPLIT"),
    "Claris Connect Release Notes": ("claris-connect", "connect-release-notes.md"),
    "Claris Connect for Apple School Manager Guide": ("claris-connect", "connect-apple-school-manager.md"),

    # claris-studio
    "Claris Studio Help": ("claris-studio", "SPLIT"),

    # claris-customer-console
    "Claris Customer Console Help": ("claris-customer-console", "customer-console.md"),

    # claris-mcp
    "Claris MCP Help": ("claris-mcp", "mcp-help.md"),

    # filemaker-pro (multiple sub-sections fold in)
    "Claris FileMaker Pro Help": ("filemaker-pro", "SPLIT"),
    "Claris FileMaker Pro Installation Guide": ("filemaker-pro", "pro-installation.md"),
    "Claris FileMaker Pro Network Install Setup Guide": ("filemaker-pro", "pro-network-install.md"),
    "Claris FileMaker Pro Release Notes": ("filemaker-pro", "pro-release-notes.md"),
    "Claris FileMaker Pro SVG Grammar for Button Icons": ("filemaker-pro", "pro-svg-grammar.md"),
}


# ---------------------------------------------------------------------------
# Split classifiers
#
# For sections marked "SPLIT" in SECTION_MAP, these functions classify each
# link into a sub-file based on the URL slug. Each returns a (filename, title)
# tuple. Pages that don't match any rule fall into a catch-all file.
# ---------------------------------------------------------------------------

def classify_connect_reference(slug):
    """Classify Claris Connect Reference pages into thematic sub-files."""

    UTILITIES = {
        "approvals", "calculations", "cryptography", "dates", "documents",
        "http", "images", "json", "lists", "schedules", "text", "variable",
        "webhooks",
    }

    CLARIS_ECOSYSTEM = {
        "filemaker_connector", "filemaker_cloud", "filemaker_cloud_admin",
        "filemaker_admin", "filemaker_server", "on_premise_filemaker",
        "claris_studio",
    }

    CLOUD_STORAGE = {
        "aws_lambda", "aws_s3", "aws_ses", "azure_ad", "box", "dropbox",
        "dropbox_sign", "ftp", "sftp", "google_drive", "sharepoint",
    }

    COMMUNICATION = {
        "chatwork", "mailchimp", "mailgun", "mailparser", "mediasms",
        "office365", "microsoft_teams", "pubnub", "sendgrid", "slack",
        "twilio", "twitter",
    }

    AI_SPECIALIZED = {
        "clarifai", "openai", "monkeylearn", "smmry",
        "jamf", "oneroster", "apple_school_manager", "particle",
        "cloudsign", "ekispert", "itrust", "smarthr", "navitime",
        "postcode_jp",
    }

    # Extract the base connector from the slug (before any hyphenated sub-page)
    # e.g., "shopify-create-product-image.md" → "shopify"
    # e.g., "google_spreadsheet-data-add-row.md" → "google_spreadsheet"
    base = slug.replace(".md", "")
    # Sub-pages use connector-action format; split on first hyphen that follows
    # an underscore-style connector name
    for known_set in [UTILITIES, CLARIS_ECOSYSTEM, CLOUD_STORAGE, COMMUNICATION, AI_SPECIALIZED]:
        for connector in known_set:
            if base == connector or base.startswith(connector + "-"):
                if connector in UTILITIES:
                    return ("connect-ref-utilities.md", "Claris Connect Reference — Built-in Utilities")
                elif connector in CLARIS_ECOSYSTEM:
                    return ("connect-ref-claris.md", "Claris Connect Reference — Claris Ecosystem")
                elif connector in CLOUD_STORAGE:
                    return ("connect-ref-cloud-storage.md", "Claris Connect Reference — Cloud & Storage")
                elif connector in COMMUNICATION:
                    return ("connect-ref-communication.md", "Claris Connect Reference — Communication & Messaging")
                elif connector in AI_SPECIALIZED:
                    return ("connect-ref-ai-specialized.md", "Claris Connect Reference — AI & Specialized Services")

    # Catch-all: business apps (CRM, e-commerce, project management, etc.)
    return ("connect-ref-business-apps.md", "Claris Connect Reference — Business Apps")


def classify_server_help(slug):
    """Classify FileMaker Server Help pages into thematic sub-files.

    Actual slug patterns:
      config-* — configuration/settings pages
      monitor-* — monitoring and log pages
      schedule-*, sched-* — backup/schedule pages
      databases-* — database operations
      hostdb-*, hostsite-* — hosting
      trouble-* — troubleshooting
      config-ai-* — AI services
      config-auth-*, config-csr-*, ssl-*, security.*, permissions-* — security
      plugins-* — plug-in management
    """

    base = slug.replace(".md", "")

    # AI services
    if base.startswith("config-ai-"):
        return ("server-ai.md", "FileMaker Server Help — AI Services")

    # Security & authentication
    if (base.startswith("config-auth-") or base.startswith("config-csr-") or
            base.startswith("ssl-") or base.startswith("permissions-") or
            base in ("security", "private-key", "hostdb-accounts")):
        return ("server-security.md", "FileMaker Server Help — Security & Authentication")

    # Backups & schedules
    if (base.startswith("schedule-") or base.startswith("sched-") or
            base.startswith("config-backup-") or base.startswith("config-restore-") or
            base.startswith("settings-")):
        return ("server-backups-schedules.md", "FileMaker Server Help — Backups & Schedules")

    # Monitoring & logs
    if (base.startswith("monitor-") or base == "event-viewer-win" or
            base == "dashboard" or base.startswith("dashboard-")):
        return ("server-monitoring-logs.md", "FileMaker Server Help — Monitoring & Logs")

    # Databases & hosting
    if (base.startswith("databases-") or base.startswith("hostdb-") or
            base.startswith("hostsite-") or base == "containerdatafolder" or
            base.startswith("config-dbserver-") or base.startswith("config-folders-") or
            base == "admin-databases" or base.startswith("config-web") or
            base.startswith("xdbc-") or base.startswith("xbdc-")):
        return ("server-databases-hosting.md", "FileMaker Server Help — Databases & Hosting")

    # Troubleshooting & maintenance
    if (base.startswith("trouble-") or base == "trouble" or
            base == "deploy-fm-server-test" or base.startswith("plugins-")):
        return ("server-troubleshooting.md", "FileMaker Server Help — Troubleshooting & Maintenance")

    # Catch-all: admin console, general settings, startup, licensing, clients
    return ("server-admin-settings.md", "FileMaker Server Help — Admin Console & Settings")


def classify_studio_help(slug):
    """Classify Claris Studio Help pages into thematic sub-files.

    Actual slug patterns:
      func-abs.md, func-if.md, etc. — functions
      field-address.md, field-date.md, etc. — fields
      calc-functions-reference.md, calculations-primer.md — functions ref
    """

    base = slug.replace(".md", "")

    # Functions: func-* prefix plus the reference/primer pages
    if base.startswith("func-") or base in ("calc-functions-reference", "calculations-primer"):
        return ("studio-functions.md", "Claris Studio Help — Functions")

    # Fields: field-* prefix plus the "fields" overview
    if base.startswith("field-") or base == "fields":
        return ("studio-fields.md", "Claris Studio Help — Fields")

    # Administration: identity providers, user/team management, licensing
    ADMIN_SLUGS = {
        "about-user-roles", "change-teams-name", "change-users-name",
        "invite-users", "promote-to-team-manager", "remove-users",
        "claris-licensing", "work-with-groups", "troubleshooting-inviting-users",
        "external-idp-auth", "why-use-external-idp",
    }
    ADMIN_PREFIXES = ("adfs-", "amazon-cognito", "google-workspace", "microsoft-entra", "okta-")

    if base in ADMIN_SLUGS:
        return ("studio-administration.md", "Claris Studio Help — Administration")
    for prefix in ADMIN_PREFIXES:
        if base.startswith(prefix):
            return ("studio-administration.md", "Claris Studio Help — Administration")

    # Catch-all: views, objects, data sources, getting started
    return ("studio-views-objects.md", "Claris Studio Help — Views & Objects")


def classify_pro_help(slug):
    """Classify FileMaker Pro Help pages into thematic sub-files.

    Classification priority:
    1. Functions (by Claris category)
    2. Script steps (by Claris category)
    3. Script triggers
    4. Conceptual pages (by topic)
    """

    base = slug.replace(".md", "")

    # --- Functions ---
    # Get functions: get-* slugs (140 pages)
    if base.startswith("get-") and base != "get-folder-path":
        return ("pro-func-get.md", "FileMaker Pro — Get Functions")

    # Text functions
    TEXT_FUNCS = {
        "char", "code", "exact", "filter", "filtervalues", "getascss", "getasdate",
        "getasnumber", "getassvg", "getastext", "getastime", "getastimestamp",
        "getasurlencoded", "getvalue", "left", "leftvalues", "leftwords", "length",
        "lower", "middle", "middlevalues", "middlewords", "patterncount", "position",
        "proper", "quote", "replace", "right", "rightvalues", "rightwords",
        "serialincrement", "sortvalues", "substitute", "trim", "trimall",
        "uniquevalues", "upper", "valuecount", "wordcount",
    }
    if base in TEXT_FUNCS:
        return ("pro-func-text.md", "FileMaker Pro — Text Functions")

    # Text formatting functions
    TEXT_FMT_FUNCS = {
        "rgb", "textcolor", "textcolorremove", "textfont", "textfontremove",
        "textformatremove", "textsize", "textsizeremove", "textstyleadd",
        "textstyleremove",
    }
    if base in TEXT_FMT_FUNCS:
        return ("pro-func-text-formatting.md", "FileMaker Pro — Text Formatting Functions")

    # Number functions
    NUMBER_FUNCS = {
        "abs", "ceiling", "combination", "div", "exp", "factorial", "floor",
        "int", "lg", "ln", "log", "mod", "random", "round", "setprecision",
        "sign", "sqrt", "truncate",
    }
    if base in NUMBER_FUNCS:
        return ("pro-func-number.md", "FileMaker Pro — Number Functions")

    # Date functions
    DATE_FUNCS = {
        "date", "day", "dayname", "dayofweek", "dayofyear", "month", "monthname",
        "weekofyear", "weekofyearfiscal", "year",
    }
    if base in DATE_FUNCS:
        return ("pro-func-date.md", "FileMaker Pro — Date Functions")

    # Time functions
    TIME_FUNCS = {"hour", "minute", "seconds", "time"}
    if base in TIME_FUNCS:
        return ("pro-func-time.md", "FileMaker Pro — Time Functions")

    # Timestamp functions
    if base == "timestamp":
        return ("pro-func-timestamp.md", "FileMaker Pro — Timestamp Functions")

    # Container functions
    CONTAINER_FUNCS = {
        "base64decode", "base64encode", "base64encoderfc", "cryptauthcode",
        "cryptdecrypt", "cryptdecryptbase64", "cryptdigest", "cryptencrypt",
        "cryptencryptbase64", "cryptgeneratesignature", "cryptverifysignature",
        "getcontainerattribute", "getheight", "getlivetext", "getlivetextasjson",
        "gettextfrompdf", "getthumbnail", "getwidth", "hexdecode", "hexencode",
        "readqrcode", "textdecode", "textencode", "verifycontainer",
    }
    if base in CONTAINER_FUNCS:
        return ("pro-func-container.md", "FileMaker Pro — Container Functions")

    # Japanese functions
    JAPANESE_FUNCS = {
        "daynamej", "furigana", "hiragana", "kanahankaku", "kanazenkaku",
        "kanjinumeral", "katakana", "monthnamej", "numtojtext", "romanhankaku",
        "romanzenkaku", "yearname",
    }
    if base in JAPANESE_FUNCS:
        return ("pro-func-japanese.md", "FileMaker Pro — Japanese Functions")

    # JSON functions
    JSON_FUNCS = {
        "jsondeleteelement", "jsonformatelements", "jsongetelement",
        "jsongetelementtype", "jsonlistkeys", "jsonlistvalues", "jsonmakearray",
        "jsonparse", "jsonparsedstate", "jsonsetelement",
    }
    if base in JSON_FUNCS:
        return ("pro-func-json.md", "FileMaker Pro — JSON Functions")

    # Aggregate functions
    AGGREGATE_FUNCS = {
        "average", "count", "list", "max", "min", "stdev", "stdevp", "sum",
        "variance", "variancep",
    }
    if base in AGGREGATE_FUNCS:
        return ("pro-func-aggregate.md", "FileMaker Pro — Aggregate Functions")

    # Repeating functions
    REPEATING_FUNCS = {"extend", "getrepetition", "last"}
    if base in REPEATING_FUNCS:
        return ("pro-func-repeating.md", "FileMaker Pro — Repeating Functions")

    # Financial functions
    FINANCIAL_FUNCS = {"fv", "npv", "pmt", "pv"}
    if base in FINANCIAL_FUNCS:
        return ("pro-func-financial.md", "FileMaker Pro — Financial Functions")

    # Trigonometric functions
    TRIG_FUNCS = {"acos", "asin", "atan", "cos", "degrees", "pi", "radians", "sin", "tan"}
    if base in TRIG_FUNCS:
        return ("pro-func-trigonometric.md", "FileMaker Pro — Trigonometric Functions")

    # Logical functions
    LOGICAL_FUNCS = {
        "case", "choose", "evaluate", "evaluationerror", "executesql", "executesqle",
        "getasboolean", "getfield", "getnthrecord", "getsummary", "if-function",
        "isempty", "isvalid", "isvalidexpression", "let", "lookup", "lookupnext",
        "self", "set-recursion", "while",
    }
    if base in LOGICAL_FUNCS:
        return ("pro-func-logical.md", "FileMaker Pro — Logical Functions")

    # AI functions
    AI_FUNCS = {
        "addembeddings", "compute-model", "cosinesimilarity", "getembedding",
        "getembeddingasfile", "getembeddingastext", "getfieldsonlayout",
        "getmodelattributes", "getragspaceinfo", "gettableddl", "gettokencount",
        "normalizeembedding", "predictfrommodel", "subtractembeddings",
    }
    if base in AI_FUNCS:
        return ("pro-func-ai.md", "FileMaker Pro — Artificial Intelligence Functions")

    # Design functions
    DESIGN_FUNCS = {
        "basetableids", "basetablenames", "databasenames", "fieldbounds",
        "fieldcomment", "fieldids", "fieldnames", "fieldrepetitions", "fieldstyle",
        "fieldtype", "getnextserialvalue", "layoutids", "layoutnames",
        "layoutobjectnames", "layoutobjectuuid", "relationinfo", "scriptids",
        "scriptnames", "tableids", "tablenames", "valuelistids", "valuelistitems",
        "valuelistnames", "windownames",
    }
    if base in DESIGN_FUNCS:
        return ("pro-func-design.md", "FileMaker Pro — Design Functions")

    # Mobile functions
    MOBILE_FUNCS = {"getavplayerattribute", "getsensor", "location", "locationvalues", "rangebeacons"}
    if base in MOBILE_FUNCS:
        return ("pro-func-mobile.md", "FileMaker Pro — Mobile Functions")

    # Miscellaneous functions
    MISC_FUNCS = {
        "convert-from-filemaker-path", "convert-to-filemaker-path", "getaddoninfo",
        "getbasetablename", "getfieldname", "getlayoutobjectattribute",
        "getlayoutobjectownerinfo", "getrecordidsfromfoundset",
    }
    if base in MISC_FUNCS:
        return ("pro-func-miscellaneous.md", "FileMaker Pro — Miscellaneous Functions")

    # Function category/overview pages (go with the functions)
    FUNC_CATEGORY_PAGES = {
        "aggregate-functions", "artificial-intelligence-functions",
        "container-functions", "date-functions", "design-functions",
        "financial-functions", "functions-reference", "functions",
        "get-functions", "json-functions-category", "json-functions",
        "logical-functions", "miscellaneous-functions", "mobile-functions",
        "number-functions", "repeating-functions", "text-formatting-functions",
        "text-functions", "time-functions", "timestamp-functions",
        "trigonometric-functions", "japanese-functions",
        "custom-functions", "custom-function-dependency",
        "importing-custom-functions", "using-custom-functions",
        "working-with-formulas-functions", "formulas",
    }
    if base in FUNC_CATEGORY_PAGES:
        return ("pro-func-reference.md", "FileMaker Pro — Functions Reference & Overview")

    # --- Script steps ---
    # Control script steps
    CONTROL_STEPS = {
        "allow-user-abort", "commit-transaction", "configure-local-notification",
        "configure-nfc", "configure-region-monitor-script",
        "else", "else-if", "end-if", "end-loop", "exit-loop-if", "exit-script",
        "halt-script", "if-script-step", "install-ontimer-script", "loop",
        "open-transaction", "pause-resume-script", "perform-script",
        "perform-script-on-server", "perform-script-on-server-callback",
        "revert-transaction", "set-error-capture", "set-error-logging",
        "set-layout-object-animation", "set-revert-transaction-on-error",
        "set-variable", "trigger-claris-connect-flow",
    }
    if base in CONTROL_STEPS:
        return ("pro-steps-control.md", "FileMaker Pro — Control Script Steps")

    # Navigation script steps
    NAV_STEPS = {
        "close-popover", "enter-browse-mode", "enter-find-mode",
        "enter-preview-mode", "go-to-field", "go-to-layout",
        "go-to-list-of-records", "go-to-next-field", "go-to-object",
        "go-to-portal-row", "go-to-previous-field",
        "go-to-record-request-page", "go-to-related-record",
    }
    if base in NAV_STEPS:
        return ("pro-steps-navigation.md", "FileMaker Pro — Navigation Script Steps")

    # Editing script steps
    EDITING_STEPS = {
        "clear", "copy", "cut", "paste", "perform-find-replace",
        "select-all", "set-selection", "undo-redo",
    }
    if base in EDITING_STEPS:
        return ("pro-steps-editing.md", "FileMaker Pro — Editing Script Steps")

    # Fields script steps
    FIELDS_STEPS = {
        "export-field-contents", "insert-audio-video", "insert-calculated-result",
        "insert-current-date", "insert-current-time", "insert-current-user-name",
        "insert-file", "insert-from-device", "insert-from-index",
        "insert-from-last-visited", "insert-from-url", "insert-pdf",
        "insert-picture", "insert-text", "relookup-field-contents",
        "replace-field-contents", "set-field", "set-field-by-name",
        "set-next-serial-value",
    }
    if base in FIELDS_STEPS:
        return ("pro-steps-fields.md", "FileMaker Pro — Fields Script Steps")

    # Records script steps
    RECORDS_STEPS = {
        "commit-records-requests", "copy-all-records-requests",
        "copy-record-request", "delete-all-records", "delete-portal-row",
        "delete-record-request", "duplicate-record-request", "export-records",
        "import-records", "new-record-request", "open-record-request",
        "revert-record-request", "save-records-as-excel", "save-records-as-jsonl",
        "save-records-as-pdf", "save-records-as-snapshot-link", "truncate-table",
    }
    if base in RECORDS_STEPS:
        return ("pro-steps-records.md", "FileMaker Pro — Records Script Steps")

    # Found Sets script steps
    FOUND_SETS_STEPS = {
        "constrain-found-set", "extend-found-set", "find-matching-records",
        "modify-last-find", "omit-multiple-records", "omit-record",
        "perform-find", "perform-quick-find", "show-all-records",
        "show-omitted-only", "sort-records", "sort-records-by-field",
        "unsort-records",
    }
    if base in FOUND_SETS_STEPS:
        return ("pro-steps-found-sets.md", "FileMaker Pro — Found Sets Script Steps")

    # Windows script steps
    WINDOWS_STEPS = {
        "adjust-window", "arrange-all-windows", "close-window", "freeze-window",
        "move-resize-window", "new-window", "refresh-window", "scroll-window",
        "select-window", "set-window-title", "set-zoom-level",
        "show-hide-menubar", "show-hide-text-ruler", "show-hide-toolbars",
        "view-as",
    }
    if base in WINDOWS_STEPS:
        return ("pro-steps-windows.md", "FileMaker Pro — Windows Script Steps")

    # Files script steps
    FILES_STEPS = {
        "close-data-file", "close-file", "convert-file", "create-data-file",
        "delete-file", "get-data-file-position", "get-file-exists",
        "get-file-size", "new-file", "open-data-file", "open-file", "print",
        "print-setup", "read-from-data-file", "recover-file", "rename-file",
        "save-a-copy-as", "save-a-copy-as-xml", "set-data-file-position",
        "set-multi-user", "set-use-system-formats", "write-to-data-file",
    }
    if base in FILES_STEPS:
        return ("pro-steps-files.md", "FileMaker Pro — Files Script Steps")

    # Accounts script steps
    ACCOUNTS_STEPS = {
        "add-account", "change-password", "delete-account", "enable-account",
        "re-login", "reset-account-password",
    }
    if base in ACCOUNTS_STEPS:
        return ("pro-steps-accounts.md", "FileMaker Pro — Accounts Script Steps")

    # AI script steps
    AI_STEPS = {
        "configure-ai-account", "configure-machine-learning-model",
        "configure-prompt-template", "configure-rag-account",
        "configure-regression-model", "fine-tune-model",
        "generate-response-from-model", "insert-embedding",
        "insert-embedding-in-found-set", "perform-find-by-natural-language",
        "perform-rag-action", "perform-semantic-find",
        "perform-sql-query-by-natural-language", "set-ai-call-logging",
    }
    if base in AI_STEPS:
        return ("pro-steps-ai.md", "FileMaker Pro — Artificial Intelligence Script Steps")

    # Spelling script steps
    SPELLING_STEPS = {
        "check-found-set", "check-record", "check-selection", "correct-word",
        "edit-user-dictionary", "select-dictionaries", "set-dictionary",
        "spelling-options",
    }
    if base in SPELLING_STEPS:
        return ("pro-steps-spelling.md", "FileMaker Pro — Spelling Script Steps")

    # Open Menu Item script steps
    OPEN_MENU_STEPS = {
        "open-edit-saved-finds", "open-favorites", "open-file-options",
        "open-find-replace", "open-help", "open-hosts", "open-manage-containers",
        "open-manage-data-sources", "open-manage-database", "open-manage-layouts",
        "open-manage-themes", "open-manage-value-lists", "open-settings",
        "open-script-workspace", "open-sharing", "open-upload-to-host",
    }
    if base in OPEN_MENU_STEPS:
        return ("pro-steps-open-menu-item.md", "FileMaker Pro — Open Menu Item Script Steps")

    # Miscellaneous script steps
    MISC_STEPS = {
        "comment", "allow-formatting-bar", "avplayer-play", "avplayer-set-options",
        "avplayer-set-playback-state", "beep", "dial-phone",
        "enable-touch-keyboard", "execute-filemaker-data-api", "execute-sql-step",
        "exit-application", "flush-cache-to-disk", "get-folder-path",
        "install-menu-set", "install-plug-in-file", "open-url",
        "perform-applescript-os-x", "perform-javascript-in-web-viewer",
        "refresh-object", "refresh-portal", "save-a-copy-as-add-on-package",
        "send-dde-execute-windows", "send-event", "send-mail",
        "set-session-identifier", "set-web-viewer", "show-custom-dialog",
        "speak-os-x",
    }
    if base in MISC_STEPS:
        return ("pro-steps-miscellaneous.md", "FileMaker Pro — Miscellaneous Script Steps")

    # Script step category/overview pages
    STEP_CATEGORY_PAGES = {
        "accounts-script-steps", "artificial-intelligence-script-steps",
        "control-script-steps", "disabling-script-steps", "editing-script-steps",
        "fields-script-steps", "files-script-steps", "found-sets-script-steps",
        "miscellaneous-script-steps", "navigation-script-steps",
        "open-menu-item-script-steps", "records-script-steps",
        "script-steps-reference", "spelling-script-steps", "windows-script-steps",
    }
    if base in STEP_CATEGORY_PAGES:
        return ("pro-steps-reference.md", "FileMaker Pro — Script Steps Reference & Overview")

    # --- Script triggers ---
    TRIGGER_PAGES = {
        "actions-dont-activate-triggers", "script-triggers",
        "script-triggers-layouts", "script-triggers-objects",
        "script-triggers-reference", "file-script-triggers",
        "layout-script-triggers", "object-script-triggers",
    }
    if base in TRIGGER_PAGES or base.startswith("on"):
        return ("pro-script-triggers.md", "FileMaker Pro — Script Triggers")

    # --- Conceptual/task pages ---

    # Layouts
    LAYOUT_SLUGS = {
        "adding-changing-field-labels", "adding-fields-to-layout",
        "adding-layout-part", "adding-popover", "adding-slide-control",
        "adding-tab-control", "adding-text", "arranging-objects",
        "auto-resize-options", "badges", "best-practices-designing-layouts",
        "borders-fill-baselines", "buttons-button-bars", "changing-layout-part",
        "changing-popover-settings", "changing-slide-control",
        "changing-tab-control", "changing-table-for-layout", "changing-the-theme",
        "changing-the-width", "conditional-formatting", "controlling-animations",
        "creating-layout", "damaged-layouts", "defining-changing-button",
        "defining-changing-button-bar", "display-calendar", "display-keyboard",
        "display-repeating-fields", "display-state", "drawing-inserting-objects",
        "editing-creating-styles", "editing-layouts", "field-borders-fill",
        "filling-with-color-gradient", "filling-with-image",
        "format-objects", "formats-container-fields", "formats-number-fields",
        "formatting-attributes", "formatting-button-bars", "formatting-buttons-popovers",
        "formatting-fields", "formatting-graphics", "formatting-panel-controls",
        "formatting-placeholder-text", "formatting-portals",
        "formatting-scaling-axes", "formatting-text", "guides",
        "header-footer", "hiding-showing-objects", "inserting-graphics-onto-a-layout",
        "inserting-layout-calculations-on-layout",
        "inserting-merge-variables-on-layout", "inserting-variables-on-layout",
        "label-contents", "layout-part-types", "layout-parts",
        "layout-pop-up-menu", "layout-preferences", "layout-tools",
        "layout-types", "layouts-and-reports", "managing-layouts",
        "merge-fields", "moving-objects", "new-layout-report",
        "objects-on-panel-controls", "page-breaks-numbering", "page-margins",
        "panel-controls", "placing-removing-fields", "popovers",
        "protecting-objects-from-change", "resizing-layout-parts",
        "rulers-and-grid", "screen-stencils", "scroll-bars",
        "setting-fill-line-style-borders", "showing-hiding-field-frames",
        "switching-layouts", "tab-order", "text-formats", "tooltips",
        "vertical-writing", "viewing-applying-styles", "web-viewers",
        "navigating-in-web-viewers", "window-styles", "working-with-layout-objects",
        "zoom-controls",
    }
    if base in LAYOUT_SLUGS:
        return ("pro-layouts.md", "FileMaker Pro — Layouts & Objects")

    # Scripting concepts (not individual steps, but how-to)
    SCRIPTING_SLUGS = {
        "create-script-for-report", "creating-editing-scripts",
        "creating-file-paths", "debugging-scripts", "managing-scripts-folders",
        "options-for-starting-scripts", "paths-in-server-side-scripts",
        "running-scripts-on-server", "script-examples", "scripts",
        "scripts-menu", "scripting-activex", "scripting-apple-events",
        "scripting-javascript-in-web-viewers",
    }
    if base in SCRIPTING_SLUGS:
        return ("pro-scripting.md", "FileMaker Pro — Scripting Concepts")

    # Fields & data entry
    FIELDS_DATA_SLUGS = {
        "adding-duplicating-deleting-records", "adding-viewing-data",
        "allowing-preventing-field-entry", "auto-complete", "automatic-data-entry",
        "automatic-record-saving", "calculation-evaluation-context",
        "calculation-fields", "changing-field-types", "choosing-a-field-type",
        "committing-data", "container-fields", "contents-of-repetition",
        "copying-moving-data", "data-in-container-fields", "data-in-date-fields",
        "data-in-table-view", "data-in-time-fields", "data-input-behavior",
        "data-viewer", "database-fields", "database-tables", "adding-tables",
        "date-fields", "dates-with-two-digit-years", "defining-fields-fields-tab",
        "defining-fields-table-view", "deleting-table-field-data",
        "entering-changing-data", "entering-data", "entering-data-from-value-list",
        "exiting-a-field", "external-storage-container-data",
        "field-indexes", "field-indexing-options", "field-validation",
        "fields-in-table-view", "global-fields", "inserting-variables-into-fields",
        "naming-fields", "number-fields", "pdf-in-container", "repeating-fields",
        "replacing-field-contents", "selecting-a-field", "setting-field-control",
        "setting-field-options", "store-data-externally", "text-fields",
        "time-fields", "timestamp-fields", "transferring-container-data",
        "validating-data-import", "audio-video-containers", "value-lists",
        "example-value-list",
    }
    if base in FIELDS_DATA_SLUGS:
        return ("pro-fields-data.md", "FileMaker Pro — Fields & Data Entry")

    # Relationships
    RELATIONSHIP_SLUGS = {
        "creating-relationships", "many-to-many-relationships",
        "multi-criteria-relationships", "one-to-many-relationships",
        "one-to-one-relationships", "relationship-criteria", "relationships",
        "relationships-comparative-operators", "relationships-return-range",
        "related-field-placement", "related-tables-files",
        "working-relationships-graph", "creating-portals",
        "selecting-working-portals", "working-with-portals", "portals",
        "lookups",
    }
    if base in RELATIONSHIP_SLUGS:
        return ("pro-relationships.md", "FileMaker Pro — Relationships & Portals")

    # Security
    SECURITY_SLUGS = {
        "about-claris-id", "about-the-admin-and-guest-accounts",
        "accounts-privilege-sets-privileges", "authorizing-access",
        "changing-account-access-priority", "changing-password-for-file",
        "controlling-open-quickly-access", "creating-editing-accounts",
        "creating-editing-privilege-sets", "disconnecting-idle-users",
        "editing-apple-id-accounts", "editing-claris-id-external-idp-accounts",
        "editing-external-server-accounts", "editing-filemaker-file-accounts",
        "editing-oauth-accounts", "encrypting-database-files",
        "extended-privileges", "layouts-privileges", "managing-extended-privileges",
        "oauth2-options", "other-privileges", "password-protected-files",
        "password-protecting", "planning-security", "predefined-privilege-sets",
        "protecting-databases", "record-access-privileges", "scripts-privileges",
        "security-lock-icons", "value-list-privileges",
    }
    if base in SECURITY_SLUGS:
        return ("pro-security.md", "FileMaker Pro — Security & Accounts")

    # Finding & sorting records
    FIND_SORT_SLUGS = {
        "configuring-quick-find", "constraining-narrowing-found-set",
        "extending-broadening-found-set", "find-request",
        "finding-duplicate-values", "finding-duplicate-values-self-join",
        "finding-empty-non-empty-fields", "finding-equal-values-different-fields",
        "finding-numbers-dates-times-timestamps", "finding-ranges",
        "finding-records", "finding-records-multiple-criteria",
        "finding-records-single-criteria", "finding-replacing-data",
        "finding-text", "hiding-viewing-records", "managing-find-requests",
        "omitting-records", "organize-records", "performing-a-quick-find",
        "saving-find-request", "selecting-current-record", "sorting-records",
        "sorting-records-options", "specify-edit-find-requests",
        "moving-through-records", "viewing-records",
        "viewing-repeating-changing-last-find",
    }
    if base in FIND_SORT_SLUGS:
        return ("pro-finding-sorting.md", "FileMaker Pro — Finding & Sorting Records")

    # Import/export & data exchange
    IMPORT_EXPORT_SLUGS = {
        "automating-odbc-import", "comma-separated-text-format",
        "configuring-odbc-driver", "connecting-to-data-sources",
        "conversion", "converting-files", "converting-from-trial-to-full",
        "creating-new-table-import", "custom-separated-text-format",
        "dbase-iii-iv-dbf-format", "editing-external-data-sources",
        "editing-odbc-data-sources", "excel", "excel-format",
        "export-records", "exporting-data", "exporting-field-contents",
        "external-data-sources", "filemaker-pro-format", "html-table-format",
        "import-action-field-mapping", "import-export-formats",
        "importing-data", "importing-data-into-file", "importing-folder",
        "importing-scripts", "importing-themes", "importing-xml", "merge-format",
        "odbc-jdbc", "querying-odbc-source", "recurring-imports",
        "restoring-links-odbc", "saving-importing-exporting-data",
        "sharing-via-odbc-jdbc", "sql-interact-with-odbc-data-sources",
        "sql-query-importing-odbc", "storing-sql-query",
        "tab-separated-text-format", "updating-data-between-data-sources",
        "working-with-data-sources", "xml-format",
    }
    if base in IMPORT_EXPORT_SLUGS:
        return ("pro-import-export.md", "FileMaker Pro — Import, Export & Data Exchange")

    # Charts & reporting
    CHARTS_REPORTING_SLUGS = {
        "changing-chart-display", "chart-guidelines", "chart-preview",
        "chart-setup", "chart-types", "column-bar-line-area-charts",
        "creating-charts", "creating-editing-charts", "creating-portals-list-detail",
        "dynamic-reports-in-table-view", "example-charting-delimited-data",
        "formatting-scaling-axes", "include-subtotals-grand-totals",
        "pie-charts", "placing-chart-in-layout-part", "quick-charts",
        "report-considerations", "scatter-bubble-charts",
        "sort-records-for-report", "sorting-records-subsummary-values",
        "specifying-chart-data-source", "subtotals", "summary-fields",
        "options-summary-field",
    }
    if base in CHARTS_REPORTING_SLUGS:
        return ("pro-charting-reporting.md", "FileMaker Pro — Charts & Reporting")

    # Sharing, publishing & hosting
    SHARING_SLUGS = {
        "open-my-apps", "opening-files-as-client", "opening-files-url",
        "peer-to-peer-sharing", "publishing-databases-web",
        "publishing-databases-web-interactive", "publishing-databases-web-static",
        "sending-email-message", "sending-url-of-file",
        "shared-files-as-client", "sharing-files", "sharing-files-mobile-clients",
        "upload-to-server", "uploading-to-server", "smtp-options",
        "email-messages",
    }
    if base in SHARING_SLUGS:
        return ("pro-sharing.md", "FileMaker Pro — Sharing & Publishing")

    # File management, developer tools, backup & recovery
    FILE_MGMT_SLUGS = {
        "add-ons", "backing-up", "converting-to-new-file", "creating-a-custom-app",
        "creating-files", "creating-kiosk-solutions", "damaged-files",
        "developer-utilities", "developer-utilities-options", "example-backup-script",
        "favorite-files-hosts", "file-consistency", "file-options",
        "kiosk-mode", "maintaining-recovering-databases", "opening-managing-files",
        "preventing-database-damage", "recovering", "recovering-files",
        "recovery-options", "removing-admin-access", "replacing-license-certificate",
        "restoring-data", "saving-compacted-copy", "saving-copying-files",
        "saving-developer-utilities-settings", "saving-reverting-changes",
        "solutions", "snapshot-link",
    }
    if base in FILE_MGMT_SLUGS:
        return ("pro-file-management.md", "FileMaker Pro — File Management & Developer Tools")

    # Printing & previewing
    PRINTING_SLUGS = {
        "envelope-contents", "how-layouts-print", "previewing-data",
        "previewing-printing", "print-records-columns", "printer-paper-options",
        "printing-labels", "printing-records", "printing-table-field-information",
        "preventing-objects-from-printing",
    }
    if base in PRINTING_SLUGS:
        return ("pro-printing.md", "FileMaker Pro — Printing & Previewing")

    # Keyboard shortcuts
    SHORTCUTS_SLUGS = {
        "manage-database-shortcuts-os-x", "manage-database-shortcuts-windows",
        "mode-shortcuts-os-x", "mode-shortcuts-windows",
        "running-scripts-through-shortcuts", "script-debugger-shortcuts-os-x",
        "script-debugger-shortcuts-windows", "script-workspace-shortcuts-os-x",
        "script-workspace-shortcuts-windows", "shortcuts-os-x",
        "shortcuts-preferences", "shortcuts-windows",
        "text-shortcuts-os-x", "text-shortcuts-windows",
    }
    if base in SHORTCUTS_SLUGS:
        return ("pro-shortcuts.md", "FileMaker Pro — Keyboard Shortcuts")

    # Catch-all: preferences, operators, modes, value lists, custom menus,
    # plug-ins, tables, misc
    return ("pro-general.md", "FileMaker Pro — General")


def extract_slug(link_line):
    """Extract the page slug from a markdown link line."""
    match = re.search(r'/([^/]+\.md)\)', link_line)
    if match:
        return match.group(1)
    return ""


SPLIT_CLASSIFIERS = {
    "Claris Connect Reference": classify_connect_reference,
    "Claris FileMaker Server Help": classify_server_help,
    "Claris Studio Help": classify_studio_help,
    "Claris FileMaker Pro Help": classify_pro_help,
}


def fetch_llms_full(local_path=None):
    """Fetch llms-full.txt, either from a local file or the web."""
    if local_path:
        path = Path(local_path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        print(f"Reading from {path}")
        return path.read_text(encoding="utf-8")

    print(f"Fetching {LLMS_FULL_URL} ...")
    req = urllib.request.Request(LLMS_FULL_URL, headers={"User-Agent": "claris-docs-skills/update"})
    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode("utf-8")

    # Save snapshot
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    snapshot_path = SNAPSHOTS_DIR / f"llms-full-{date.today().isoformat()}.txt"
    snapshot_path.write_text(content, encoding="utf-8")
    print(f"Saved snapshot to {snapshot_path}")

    return content


def parse_english_sections(text):
    """Parse the English section of llms-full.txt into {header: [links]}."""
    sections = {}
    in_english = False
    current_header = None

    for line in text.splitlines():
        # Detect locale boundaries
        if line.startswith("## "):
            if "English" in line or "(en)" in line:
                in_english = True
            else:
                if in_english:
                    break  # Done with English
            continue

        if not in_english:
            continue

        # Section headers
        if line.startswith("### "):
            current_header = line[4:].strip()
            if current_header not in sections:
                sections[current_header] = []
            continue

        # Link entries
        if line.startswith("- [") and current_header:
            sections[current_header].append(line)

    return sections


def write_reference_file(skill_name, ref_filename, links, section_title):
    """Write a single reference file."""
    ref_dir = SKILLS_DIR / skill_name / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)

    ref_path = ref_dir / ref_filename
    lines = [
        "<!-- DO NOT EDIT — generated by update.py from llms-full.txt -->\n",
        f"# {section_title}\n",
        "",
    ]
    for link in links:
        lines.append(link)

    ref_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ref_path


def collect_existing_reference_files():
    """Return a set of all existing reference file paths across all skills."""
    existing = set()
    for ref_file in SKILLS_DIR.glob("*/references/*.md"):
        existing.add(ref_file)
    return existing


def main():
    local_path = sys.argv[1] if len(sys.argv) > 1 else None
    live_run = local_path is None

    # Snapshot existing files before writing so we can detect stale ones.
    # Only do this on live runs — local snapshot runs are partial by design
    # and would incorrectly flag unrelated reference files for deletion.
    existing_before = collect_existing_reference_files() if live_run else set()

    text = fetch_llms_full(local_path)
    sections = parse_english_sections(text)

    print(f"Found {len(sections)} English sections")

    # Track what we write for summary
    written = {}
    written_paths = set()
    unmapped = []

    for header, links in sections.items():
        if header not in SECTION_MAP:
            unmapped.append(header)
            continue

        skill_name, ref_filename = SECTION_MAP[header]

        if ref_filename == "SPLIT":
            # Use classifier to distribute links across sub-files
            classifier = SPLIT_CLASSIFIERS[header]
            buckets = {}  # {(filename, title): [links]}
            for link in links:
                slug = extract_slug(link)
                file_key = classifier(slug)
                if file_key not in buckets:
                    buckets[file_key] = []
                buckets[file_key].append(link)

            for (sub_filename, sub_title), sub_links in buckets.items():
                ref_path = write_reference_file(skill_name, sub_filename, sub_links, sub_title)
                written_paths.add(ref_path)
                if skill_name not in written:
                    written[skill_name] = []
                written[skill_name].append((sub_filename, len(sub_links)))
        else:
            ref_path = write_reference_file(skill_name, ref_filename, links, header)
            written_paths.add(ref_path)
            if skill_name not in written:
                written[skill_name] = []
            written[skill_name].append((ref_filename, len(links)))

    # Remove stale reference files (live runs only)
    stale = existing_before - written_paths
    if stale:
        print(f"\nRemoving {len(stale)} stale reference file(s):")
        for stale_path in sorted(stale):
            stale_path.unlink()
            print(f"  Removed: {stale_path.relative_to(REPO_ROOT)}")

    # Summary
    print(f"\nWrote reference files for {len(written)} skills:")
    for skill, files in sorted(written.items()):
        total = sum(count for _, count in files)
        file_list = ", ".join(f"{f} ({c})" for f, c in files)
        print(f"  {skill}: {total} pages across {len(files)} files — {file_list}")

    if unmapped:
        print(f"\nUnmapped sections (no skill defined): {unmapped}")

    print("\nDone.")


if __name__ == "__main__":
    main()
