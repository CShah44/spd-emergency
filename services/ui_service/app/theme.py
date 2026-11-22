"""Colors and CSS for the page: a command-center look, with the S.P.D. rangers'
colors as the status colors. Bold flat colors, thick borders, big buttons, no effects."""

NAVY = "#0b1226"
PANEL = "#121d3d"
LINE = "#2a3d70"
TEXT = "#e6ebff"
MUTED = "#8b97c0"

CSS = f"""
body, body.body--dark, .q-page, .body--dark .q-page {{ background: {NAVY} !important; color: {TEXT}; }}
body {{ font-family: 'Bahnschrift', 'Segoe UI', system-ui, sans-serif; font-size: 16px; }}
.mono {{ font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace; }}

.topbar {{ background: #070d1f !important; border-bottom: 4px solid #ffcf1f; }}
.brand {{ font-size: 1.5rem; font-weight: 800; letter-spacing: .1em; color: #ffcf1f; }}
.brand-sub {{ font-size: .85rem; font-weight: 700; letter-spacing: .3em; color: {MUTED}; }}

.panel {{ background: {PANEL}; border: 2px solid {LINE}; border-radius: 8px; padding: 18px 20px; }}
.panel-title {{ font-size: .9rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase;
  color: {TEXT}; border-left: 6px solid #ffcf1f; padding-left: 10px; margin-bottom: 14px; }}
.muted {{ color: {MUTED}; }}
.small {{ font-size: .82rem; }}

.chip {{ display: inline-block; padding: 3px 11px; border-radius: 4px; font-size: .75rem; font-weight: 800;
  letter-spacing: .06em; color: {NAVY}; white-space: nowrap; }}
.chip-outline {{ display: inline-block; padding: 1px 9px; border: 2px solid; border-radius: 4px; font-size: .75rem;
  font-weight: 800; letter-spacing: .06em; white-space: nowrap; }}

/* tables: tall rows, bold text, a scroll box for the body */
.rows-scroll {{ overflow-x: auto; }}
.trow {{ display: grid; gap: 12px; align-items: center; padding: 12px 10px; min-height: 3.6rem;
  border-bottom: 1px solid #22336a; font-size: .95rem; }}
.trow:not(.thead):hover {{ background: rgba(47, 123, 255, .08); }}
.requests-row {{ grid-template-columns: 7.5rem 6.5rem 10rem 4.6rem 5.2rem minmax(8rem, 1fr) auto; min-width: 62rem; }}
.resources-row {{ grid-template-columns: minmax(9rem, 1.5fr) 6.5rem 7.5rem 4.6rem minmax(9rem, 1.2fr) auto; min-width: 46rem; }}
.thead {{ color: {MUTED}; font-size: .72rem; letter-spacing: .14em; text-transform: uppercase; font-weight: 800;
  min-height: 0; padding-top: 4px; padding-bottom: 8px; border-bottom: 2px solid {LINE}; }}
.scroll-box {{ max-height: 30rem; overflow-y: auto; }}
.divider-row {{ padding: 8px 10px; margin-top: 10px; font-size: .75rem; font-weight: 800; letter-spacing: .16em;
  text-transform: uppercase; color: {MUTED}; background: #0e1730; border-top: 2px solid {LINE}; border-bottom: 2px solid {LINE}; }}
.finished {{ opacity: .6; }}
.assigned {{ border-left: 5px solid #2f7bff; background: rgba(47, 123, 255, .1); }}

/* the progress bar in every request row */
.stepper {{ display: flex; gap: 4px; align-items: center; }}
.step {{ width: 26px; height: 8px; border-radius: 3px; background: {LINE}; }}
.step.done {{ opacity: .55; }}
.step.current {{ height: 12px; outline: 2px solid rgba(255, 255, 255, .35); }}

/* action buttons: filled, tall, bold */
.action-btn.q-btn {{ min-width: 6.2rem; min-height: 2.5rem; padding: 4px 16px; font-weight: 800; font-size: .85rem;
  letter-spacing: .05em; border-radius: 6px; }}
.action-btn.q-btn--outline {{ border-width: 2px; }}

/* storage nodes */
.node {{ border: 2px solid {LINE}; border-radius: 6px; padding: 12px 14px; background: #0e1730; }}
.node.leader {{ border-color: #ffcf1f; }}
.node-name {{ font-size: 1.05rem; font-weight: 800; }}
.big-number {{ font-size: 2.3rem; font-weight: 800; line-height: 1; }}

/* requests by stage */
.pipeline {{ display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap; }}
.arrow {{ align-self: center; color: {MUTED}; font-size: 1.5rem; font-weight: 800; }}
.stat-block {{ text-align: center; padding: 10px 16px; border: 2px solid; border-radius: 8px; min-width: 6.5rem;
  background: #0e1730; }}
.stat-block .big-number {{ font-size: 2rem; }}

.flash {{ animation: flash 1.4s ease-out; }}
@keyframes flash {{ from {{ background: rgba(255, 207, 31, .4); }} to {{ background: transparent; }} }}
@media (prefers-reduced-motion: reduce) {{ .flash {{ animation: none; }} }}

.q-field--dark .q-field__control {{ background: #0e1730; }}
"""
