"""WhatsApp Web selectors, kept in one place because they drift often.

Every value here was verified against the live site on 2026-08-28 by dumping
the DOM of a logged-in session. Each list is tried in order, first match wins.
When every candidate misses we report an unknown state rather than guessing --
the tool still works, it just says less.
"""

# --- session state ---------------------------------------------------------

# Chat list pane: present only once the account is linked.
LOGGED_IN = (
    '[data-testid="chat-list"]',
    "#pane-side",
    '[aria-label="Chat list"]',
    '[data-testid="wa-web-main-screen"]',
)

# The QR screen. `link-device-qr-code` and the stay-logged-in checkbox are the
# stable pair; the older two are kept as fallbacks.
LOGGED_OUT = (
    '[data-testid="link-device-qr-code"]',
    "#auto-logout-toggle",
    "div[data-ref]",
    'canvas[aria-label*="scan" i]',
)

# --- interstitials ---------------------------------------------------------

# "What's new on WhatsApp Web" and friends: a role=dialog that swallows clicks
# meant for the nav rail or the menu. Shown unpredictably after updates.
INTERSTITIAL_DISMISS = (
    '[data-testid="confirm-popup"] button:has-text("Continue")',
    '[data-testid="confirm-popup"] button[aria-label="Close"]',
    '[role="dialog"] button:has-text("Continue")',
    '[role="dialog"] button[aria-label="Close"]',
)

# The "Message notifications are off" strip above the chat list.
BUTTERBAR_DISMISS = ('[data-testid="chat-butterbar"] button[aria-label="Close"]',)

INTERSTITIAL_PRESENT = ('[role="dialog"]', '[data-testid="confirm-popup"]')

# --- logout ----------------------------------------------------------------

# Verified: the three-dot Menu in the chat-list header, NOT a settings rail item.
MENU_BUTTONS = (
    '[aria-label="Menu"]',
    '[data-testid="menu-bar-menu"]',
    '[data-testid="chatlist-header"] button[aria-label="Menu"]',
)

LOGOUT_ITEMS = (
    '[role="menuitem"][aria-label="Log out"]',
    '[data-testid="mi-logout"]',
    '[aria-label="Log out"]',
)

LOGOUT_TEXT = r"^\s*log ?out\s*$"

# --- chat list -------------------------------------------------------------

CHAT_LIST = '[data-testid="chat-list"]'
CHAT_ROWS = '[data-testid="chat-list"] [role="row"]'

# The "Unread" filter tab. Filtering is a local UI action: it does not open any
# chat and sends no read receipts.
UNREAD_FILTER_TAB = '[role="tab"]:has-text("Unread")'
ALL_FILTER_TAB = "#all-filter"

# Fields within one chat row.
ROW_TITLE = '[data-testid="cell-frame-title"]'
ROW_PREVIEW = '[data-testid="cell-frame-secondary"]'
ROW_DETAIL = '[data-testid="cell-frame-primary-detail"]'
ROW_UNREAD_BADGE = '[data-testid="icon-unread-count"]'


# --- conversation pane -----------------------------------------------------
# Verified on 2026-08-28 against a self-chat (no third party, no receipts).

CONVERSATION = "#main"
MESSAGE_ROWS = '#main [role="row"]'
MSG_CONTAINER = '[data-testid="msg-container"]'
# Carries "[HH:MM, D/M/YYYY] Sender: " -- timestamp and author in one attribute.
MSG_PRE_PLAIN = "[data-pre-plain-text]"
MSG_TEXT = '.selectable-text, [data-testid="selectable-text"]'
# Scrollable ancestor of the message list, for loading older messages.
MSG_SCROLLER = (
    '[data-testid="conversation-panel-messages"]',
    "#main div.copyable-area > div[tabindex]",
    "#main",
)


# --- composer --------------------------------------------------------------
# Verified on 2026-08-31 against the self-chat. The send control only exists
# once the composer holds text; an empty composer shows the mic instead.

COMPOSER = '[data-testid="conversation-compose-box-input"]'
SEND_BUTTON = ('button[aria-label="Send"]', '[data-testid="wds-ic-send-filled"]')
# Two independent recipient signals. Both must agree before anything is sent.
HEADER_TITLE = '[data-testid="conversation-info-header-chat-title"]'
# The composer's own aria-label reads "Type a message to <name>".
COMPOSER_ARIA_PREFIX = "Type a message to "
