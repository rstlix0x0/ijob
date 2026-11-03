"""The look of the terminal UI: one theme and the styles every screen shares.

The colors are the ones the command line draws with, in `ijob_app.view`.
"""

from typing import Final

from textual.theme import Theme

from ijob_app.view import AMBER, BORDER, CORAL, GROUND, MINT, MUTED, SELECTED, SKY, TEXT

IJOB_THEME: Final = Theme(
    name="ijob",
    primary=AMBER,
    secondary=SKY,
    accent=AMBER,
    foreground=TEXT,
    background=GROUND,
    surface=GROUND,
    panel=SELECTED,
    success=MINT,
    warning=AMBER,
    error=CORAL,
    dark=True,
)

APP_CSS: Final = f"""
* {{
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
    scrollbar-color: {BORDER};
    scrollbar-color-hover: {MUTED};
    scrollbar-color-active: {AMBER};
    scrollbar-background: {GROUND};
    scrollbar-background-hover: {GROUND};
    scrollbar-background-active: {GROUND};
    scrollbar-corner-color: {GROUND};
}}
Screen {{ background: {GROUND}; color: {TEXT}; }}
#top {{ height: 1; margin-bottom: 1; }}
#tabs {{ width: auto; height: 1; }}
#activity {{ width: 1fr; height: 1; text-align: right; }}
#panes {{ height: 1fr; }}
Footer {{ background: {GROUND}; color: {MUTED}; }}
Footer FooterKey {{ background: {GROUND}; color: {MUTED}; }}
Footer FooterKey .footer-key--key {{
    background: {MUTED}; color: {GROUND}; text-style: bold;
}}
Footer FooterKey .footer-key--description {{ color: {MUTED}; }}
Footer FooterKey:hover {{ background: {SELECTED}; }}

.box {{
    border: round {BORDER};
    border-title-color: {TEXT};
    border-title-style: bold;
    padding: 0 1;
    background: {GROUND};
}}
.box:focus-within {{ border: round {AMBER}; border-title-color: {AMBER}; }}

Input {{
    background: {GROUND};
    color: {TEXT};
    border: none;
    height: 1;
    padding: 0;
}}
Input:focus {{ border: none; background: {GROUND}; }}
Input>.input--cursor {{ background: {AMBER}; color: {GROUND}; }}
Input>.input--placeholder {{ color: {MUTED}; }}

DataTable {{ background: {GROUND}; color: {TEXT}; }}
DataTable>.datatable--header {{
    background: {GROUND}; color: {MUTED}; text-style: bold;
}}
DataTable>.datatable--cursor {{ background: {SELECTED}; color: {TEXT}; }}
DataTable>.datatable--hover {{ background: {GROUND}; }}
DataTable:focus>.datatable--cursor {{ background: {SELECTED}; color: {TEXT}; }}

Button {{
    background: {SELECTED};
    color: {TEXT};
    border: none;
    height: 1;
    min-width: 8;
    margin-right: 1;
}}
Button:hover {{ background: {BORDER}; }}
Button:focus {{ background: {AMBER}; color: {GROUND}; text-style: bold; }}

ToastRack {{ margin-bottom: 2; }}
"""
