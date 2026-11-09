/* AWTK touch view over the U2 controller. See peq_awtk_view.h and ../README.md.
 *
 * Ownership rules
 *  - Widgets: the view creates exactly one root child in the owner's parent; everything else is a
 *    descendant of that root. Only pv_close() (or the owner destroying the parent) removes them.
 *    The 64 row buttons are created once at open and reused: rescans move/relabel/hide rows and
 *    never destroy widgets, so no widget is destroyed while an event on it is being dispatched.
 *  - Callbacks: every widget_on id is stored and widget_off'ed at close. Row contexts are tokens
 *    inside pv_view; the off-pane surfaces each have their own id. A root EVT_DESTROY handler
 *    detects the owner destroying the parent first.
 *  - Deferred work: click handlers only record a request and add one idle (if none is pending).
 *    The idle runs the controller action, renders and returns RET_REMOVE. pv_close and the
 *    destroy handler remove a pending idle. There are no timers and no file operations here.
 *
 * Panes
 *   controller on   -> list UI (selected/status/list/rescan), browse flag cleared
 *   controller off  -> plain disabled pane (material + PEQ OFF + explanation) unless browse is set
 *   browse set      -> list UI while the controller stays OFF: tapping the disabled pane opens it,
 *                      selecting a preset there never enables the controller, and a failed enable
 *                      attempt also lands here so the error status is visible.
 *
 * Geometry
 *   One deterministic layout pass (layout_build) derives every rectangle from the owner's pv_rect;
 *   the area never changes while the view is open, so widgets are created with their final
 *   rectangles. The 108 px playing-button reserve applies to the header strip only. All 64 row
 *   rectangles are re-asserted on every render (visible: (0, i*row_h, list_w, row_h-4), hidden:
 *   (0, 0, list_w, 0)) before the scroll virtual height is set, because AWTK's
 *   scroll_view_on_layout_children folds invisible child geometry into the virtual size.
 *
 * Text
 *   No measuring, no guess-metrics, no truncation: every label receives the original UTF-8 string
 *   and native hscroll_label rendering/scroll decides how it is shown. */
#include "peq_awtk_view.h"

#include <stdio.h>
#include <string.h>

static const char *const off_notes[CTL_LANG_COUNT] = { PV_OFF_NOTE_EN, PV_OFF_NOTE_ZH };

const char *pv_off_label(void) { return PV_OFF_LABEL; }

const char *pv_off_note_text(ctl_lang lang) {
    return off_notes[(unsigned)lang < CTL_LANG_COUNT ? lang : CTL_LANG_EN];
}

static void set_text(pv_view *v, pv_widget *w, const char *s) { v->tk->widget_set_text_utf8(w, s); }

/* Native AWTK hit-testing can include invisible widgets. Hidden panes must also be
 * disabled, so the OFF panel cannot intercept list touches (and conversely). */
static void set_shown(pv_view *v,pv_widget *w,pv_bool on) {
    v->tk->widget_set_enable(w,on);
    v->tk->widget_set_visible(w,on);
}

/* ---------------- layout ---------------- */

static int clamp_i(int x, int lo, int hi) { return x < lo ? lo : (x > hi ? hi : x); }

/* One deterministic layout pass: every rectangle is derived from `area` and the chosen row height.
 * The 108 px playing reserve is a header fact; below the header the content uses the full width
 * with symmetric PV_SIDE margins. */
static void layout_build(pv_view *v, pv_rect area) {
    pv_layout_metrics *L = &v->lay;
    memset(L, 0, sizeof *L);
    const int aw = area.w, ah = area.h;
    const int header = PV_HEADER_H;
    const int side = PV_SIDE;
    int content_w = aw - 2 * side;
    if (content_w < 1) content_w = 1;

    L->header_y = area.y;
    L->header_h = header;
    L->toggle_w = PV_TOGGLE_W;
    L->toggle_h = header;
    L->toggle_x = aw - PV_TOGGLE_RIGHT_GAP - PV_TOGGLE_W; /* area.w - 228 => 492 at w=720 */
    L->toggle_y = 0;
    L->reset_x = aw - PV_RIGHT_PLAY_RESERVE;
    L->content_x = side;
    L->content_w = content_w;

    /* Vertical budget below the header: top gap + selected + status + list + gap + rescan +
     * bottom margin. Row height is PV_ROW_H when the area is tall enough and shrinks toward
     * PV_MIN_ROW_H for the shortest accepted areas; the viewport stays exactly PV_LIST_ROWS rows. */
    const int top_gap = 8;
    const int lbl0 = 46, lbl1 = 34, lbl2 = 78;      /* selected, compact status, rescan */
    const int max0 = 56, max1 = 34, max2 = 82;
    int span0 = lbl0, span1 = lbl1, span2 = lbl2;
    const int fixed = top_gap + span0 + span1 + PV_LIST_GAP + span2 + PV_BOTTOM_MARGIN;
    int room = ah - header - fixed;
    if (room < 0) room = 0;
    int row_h = clamp_i(room / PV_LIST_ROWS, PV_MIN_ROW_H, PV_ROW_H);
    int list_h = row_h * PV_LIST_ROWS;
    int slack = room - list_h;
    /* Spend the slack on the fixed rows first so the content fills the area naturally. */
    while (slack > 0 && (span0 < max0 || span2 < max2)) {
        if (span0 < max0) { ++span0; --slack; }
        if (slack > 0 && span2 < max2) { ++span2; --slack; }
    }
    v->row_h = row_h;

    L->content_y = header + top_gap;
    L->sel_h = span0;
    L->status_h = span1;
    L->list_x = side;
    L->list_y = L->content_y + span0 + span1;
    L->list_w = content_w;
    L->list_h = list_h;
    L->row_h = row_h;
    L->rescan_x = side;
    L->rescan_y = L->list_y + list_h + PV_LIST_GAP;
    L->rescan_w = content_w;
    L->rescan_h = span2;
    L->bottom_margin = ah - (L->rescan_y + span2);

    /* Match the stock EQ OFF coordinates relative to its 82px header. */
    L->off_pane_w = aw;
    L->off_x = (aw - PV_OFF_ICON) / 2;
    L->off_y = header + PV_OFF_ICON_Y;
    L->off_w = PV_OFF_ICON;
    L->off_h = PV_OFF_NOTE_Y - PV_OFF_ICON_Y + PV_OFF_NOTE_H;
    L->off_gap = PV_OFF_LABEL_Y - PV_OFF_ICON_Y - PV_OFF_ICON;
    L->off_icon_h = PV_OFF_ICON;
    L->off_label_h = PV_OFF_LABEL_H;
    L->off_note_h = PV_OFF_NOTE_H;

    if (v->tk && v->tk->layout_probe) v->tk->layout_probe(L, v->tk->layout_probe_ctx);
}

const pv_layout_metrics *pv_layout(const pv_view *v) { return v && v->phase != PV_CLOSED ? &v->lay : NULL; }
pv_wh pv_row_height(const pv_view *v) { return v ? v->row_h : 0; }
pv_wh pv_list_viewport_h(const pv_view *v) { return v ? v->row_h * PV_LIST_ROWS : 0; }
pv_pane pv_showing_pane(const pv_view *v) { return v ? v->showing_pane : PV_PANE_NONE; }
int pv_pane_enabled(const pv_view *v) { return pv_showing_pane(v) == PV_PANE_ON; }
int pv_browse_active(const pv_view *v) { return pv_showing_pane(v) == PV_PANE_BROWSE; }

/* Success statuses that only echo the selected preset name (already shown on the selected line).
 * Errors and warnings are never suppressed. */
static int status_echoes_selection(const ctl_model *m) {
    return m->status == CTL_ST_SAVED || m->status == CTL_ST_RESTORED || m->status == CTL_ST_ENABLED;
}

static void render(pv_view *v) {
    const pv_toolkit *tk = v->tk;
    const ctl_model *m = ctl_get_model(v->ctl);
    view_state s;
    char line[CTL_NAME_MAX + 96];
    view_render(m, v->lang, &s);

    layout_build(v, v->area);

    pv_pane want;
    if (m->enabled) {
        v->browse = 0; /* a successful enable clears browse */
        want = PV_PANE_ON;
    } else if (v->browse) {
        want = PV_PANE_BROWSE;
    } else {
        want = PV_PANE_OFF;
    }

    if (!v->pane_valid || v->showing_pane != want) {
        int list_visible = want != PV_PANE_OFF;
        set_shown(v, v->selected_label, list_visible);
        set_shown(v, v->status_label, list_visible);
        set_shown(v, v->list, list_visible);
        set_shown(v, v->rescan_btn, list_visible);
        set_shown(v, v->off_pane, !list_visible);
        set_shown(v, v->off_icon, !list_visible);
        set_shown(v, v->off_label, !list_visible);
        set_shown(v, v->off_note, !list_visible);
        v->showing_pane = want;
        v->pane_valid = 1;
        v->rows_shown = -1; /* the list shows a new pane: rebuild the viewport from the top */
    }

    /* Selected preset + compact status. The redundant "PEQ: On/Off" row is not created at all. */
    set_text(v, v->selected_label, s.selected_line);
    set_text(v, v->status_label, status_echoes_selection(m) ? "" : s.status_line);
    tk->widget_set_style_color(v->status_label, "normal:text_color",
                               s.status_is_error ? tk->text_error : tk->text_normal);
    set_text(v, v->rescan_btn, s.rescan_label);
    set_text(v, v->off_label, pv_off_label());
    set_text(v, v->off_note, pv_off_note_text(v->lang));
    if (tk->toggle_image) tk->toggle_image(v->toggle_btn, m->enabled);
    else set_text(v, v->toggle_btn, s.toggle_label);

    /* Rows: hide the tail, label the visible ones, and reset EVERY row rectangle. Hidden rows get a
     * zero-size rectangle at the origin so AWTK's layout pass cannot fold stale geometry into the
     * scroll virtual size. */
    int n = s.row_count < CTL_MAX_ENTRIES ? s.row_count : CTL_MAX_ENTRIES;
    int list_visible = v->showing_pane != PV_PANE_OFF;
    for (int i = 0; i < CTL_MAX_ENTRIES; ++i) {
        pv_widget *r = v->rows[i];
        int show = list_visible && i < n;
        if (!show) {
            set_shown(v, r, 0);
            tk->widget_move_resize(r, 0, 0, v->lay.list_w, 0);
            continue;
        }
        int sel = i == s.highlighted_row;
        snprintf(line, sizeof line, "%s%s", sel ? PV_SELECTED_MARK : "", s.rows[i]);
        set_text(v, r, line);
        tk->widget_set_style_color(r, "normal:bg_color", sel ? tk->bg_row_selected : tk->bg_row);
        set_shown(v, r, 1);
        tk->widget_move_resize(r, 0, (pv_xy)(i * v->row_h), v->lay.list_w, v->row_h - 4);
        v->tokens[i].generation = s.generation;
    }
    /* Virtual height is set after the row rectangles are final. */
    if (!v->renders || s.generation != v->rows_generation || n != v->rows_shown ||
        v->showing_pane != v->last_pane_geometry) {
        pv_wh vh = (pv_wh)(n * v->row_h);
        tk->scroll_view_set_virtual_h(v->list, vh > v->lay.list_h ? vh : v->lay.list_h);
        tk->scroll_view_set_offset(v->list, 0, 0); /* new list: start at the top */
        v->last_pane_geometry = v->showing_pane;
    }
    v->rows_shown = n;
    v->rows_generation = s.generation;
    ++v->renders;
}

static void run_pending(pv_view *v) {
    pv_action a = v->pending;
    v->pending = PV_ACT_NONE;
    switch (a) {
    case PV_ACT_RESCAN: ctl_rescan(v->ctl); break;
    case PV_ACT_SELECT: ctl_select(v->ctl, v->pending_generation, v->pending_row); break;
    case PV_ACT_BROWSE: v->browse = 1; break; /* view-only: the controller is not touched */
    case PV_ACT_TOGGLE: {
        int was = ctl_get_model(v->ctl)->enabled;
        ctl_set_enabled(v->ctl, !was);
        break;
    }
    case PV_ACT_NONE: break;
    }
    if (v->pending_reopen) {
        v->pending_reopen = 0;
        ctl_open(v->ctl);
    }
    /* Pane flags from the resulting controller state (never the other way round: this view never
     * enables or disables PEQ by itself).
     *   enabled            -> list pane, browse cleared (a successful enable clears browse)
     *   off with an error  -> browse so the failure explanation stays visible
     *   off, no error      -> keep browsing if the user was browsing, otherwise the plain off pane
     *                         (so enabled -> off returns to the original-looking disabled pane) */
    const ctl_model *m = ctl_get_model(v->ctl);
    if (m->enabled) v->browse = 0;
    else if (m->status >= CTL_ERR_DIR_MISSING) v->browse = 1;
    if (v->phase == PV_OPEN) render(v); /* a platform callback may have closed the page */
}

static pv_ret on_idle(const pv_idle_info *info) {
    pv_view *v = info->ctx;
    v->idle_id = 0; /* returning RET_REMOVE: the toolkit drops this idle itself */
    if (v->phase == PV_OPEN) run_pending(v);
    return PV_RET_REMOVE;
}

/* Ensure the request is executed by exactly one idle. Rendering never destroys widgets, so if the
 * toolkit cannot add an idle the work is done synchronously instead of being lost. */
static void schedule(pv_view *v) {
    if (v->idle_id) return;
    v->idle_id = v->tk->idle_add(on_idle, v);
    if (!v->idle_id) run_pending(v);
}

/* Click on a control of this view: record and defer. One user action at a time; clicks that arrive
 * while one is pending are ignored (the pending one reflects what the user saw). */
static void request(pv_view *v, pv_action a, unsigned generation, int row) {
    if (v->phase != PV_OPEN || v->pending != PV_ACT_NONE) return;
    v->pending = a;
    v->pending_generation = generation;
    v->pending_row = row;
    schedule(v);
}

static pv_ret on_row_click(void *ctx, pv_event *e) {
    pv_row_token *t = ctx;
    pv_view *v = t->view;
    (void)e;
    if (v->phase == PV_OPEN && t->row < v->rows_shown) request(v, PV_ACT_SELECT, t->generation, t->row);
    return PV_RET_OK;
}

static pv_ret on_rescan_click(void *ctx, pv_event *e) {
    (void)e;
    request(ctx, PV_ACT_RESCAN, 0, -1);
    return PV_RET_OK;
}

static pv_ret on_toggle_click(void *ctx, pv_event *e) {
    (void)e;
    request(ctx, PV_ACT_TOGGLE, 0, -1);
    return PV_RET_OK;
}

/* Any surface of the disabled pane opens the view-only browse list. */
static pv_ret on_off_click(void *ctx, pv_event *e) {
    (void)e;
    request(ctx, PV_ACT_BROWSE, 0, -1);
    return PV_RET_OK;
}

static void forget_widgets(pv_view *v) {
    v->root = v->selected_label = v->toggle_btn = NULL;
    v->status_label = v->list = v->rescan_btn = NULL;
    v->off_pane = v->off_icon = v->off_label = v->off_note = NULL;
    memset(v->rows, 0, sizeof v->rows);
    memset(v->row_click_id, 0, sizeof v->row_click_id);
    memset(v->off_click_id, 0, sizeof v->off_click_id);
    v->toggle_click_id = v->rescan_click_id = v->root_destroy_id = 0;
}

static void cancel_idle(pv_view *v) {
    if (v->idle_id) v->tk->idle_remove(v->idle_id);
    v->idle_id = 0;
    v->pending = PV_ACT_NONE;
    v->pending_reopen = 0;
}

/* Owner destroyed the parent (window close) without pv_close: the widgets are going away, so
 * drop every pointer and never touch them again. The emitter frees our registrations. */
static pv_ret on_root_destroy(void *ctx, pv_event *e) {
    pv_view *v = ctx;
    (void)e;
    if (v->phase != PV_OPEN) return PV_RET_OK;
    cancel_idle(v);
    forget_widgets(v);
    v->phase = PV_DETACHED;
    return PV_RET_OK;
}

static void teardown(pv_view *v) {
    const pv_toolkit *tk = v->tk;
    cancel_idle(v);
    if (v->phase == PV_OPEN && v->root) {
        for (int i = 0; i < CTL_MAX_ENTRIES; ++i)
            if (v->row_click_id[i]) tk->widget_off(v->rows[i], v->row_click_id[i]);
        if (v->toggle_click_id) tk->widget_off(v->toggle_btn, v->toggle_click_id);
        if (v->rescan_click_id) tk->widget_off(v->rescan_btn, v->rescan_click_id);
        if (v->off_click_id[0]) tk->widget_off(v->off_icon, v->off_click_id[0]);
        if (v->off_click_id[1]) tk->widget_off(v->off_label, v->off_click_id[1]);
        if (v->off_click_id[2]) tk->widget_off(v->off_note, v->off_click_id[2]);
        if (v->root_destroy_id) tk->widget_off(v->root, v->root_destroy_id);
        tk->widget_destroy(v->root);
    }
    forget_widgets(v);
    v->showing_pane = PV_PANE_NONE;
    v->pane_valid = 0;
    v->browse = 0;
    v->phase = PV_CLOSED;
}

void pv_close(pv_view *v) {
    if (!v || v->phase == PV_CLOSED) return;
    teardown(v);
}

void pv_back(pv_view *v) {
    if (!v || !v->ctl) return;
    ctl_controller *c = v->ctl;
    pv_close(v);
    ctl_back(c);
}

void pv_refresh(pv_view *v, int reopen) {
    if (!v || v->phase != PV_OPEN) return;
    if (reopen) v->pending_reopen = 1;
    schedule(v);
}

void pv_set_lang(pv_view *v, ctl_lang lang) {
    if (!v || v->phase != PV_OPEN) return;
    v->lang = (unsigned)lang < CTL_LANG_COUNT ? lang : CTL_LANG_EN;
    schedule(v);
}

/* Create one widget, name it and let the owner's decorate() apply stock material. Short-circuits
 * (leaves *ok 0) after the first failure so teardown can clear a partial tree. */
static pv_widget *mk(pv_view *v, pv_create_fn fn, pv_widget *parent, pv_xy x, pv_xy y, pv_wh w, pv_wh h,
                     const char *name, int *ok) {
    if (!*ok) return NULL;
    pv_widget *wd = fn(parent, x, y, w, h);
    if (!wd) { *ok = 0; return NULL; }
    v->tk->widget_set_name(wd, name);
    if (v->tk->decorate) v->tk->decorate(wd, name);
    return wd;
}

static uint32_t on(pv_view *v, pv_widget *w, uint32_t type, pv_event_fn fn, void *ctx, int *ok) {
    if (!*ok) return 0;
    uint32_t id = v->tk->widget_on(w, type, fn, ctx);
    if (!id) *ok = 0;
    return id;
}

int pv_open(pv_view *v, const pv_toolkit *tk, ctl_controller *ctl, pv_widget *parent, pv_rect area,
            ctl_lang lang) {
    if (!v || !tk || !ctl || !parent) return -1;
    if (v->phase == PV_OPEN) return -1;
    if (!tk->widget_move_resize) return -1; /* required: row geometry reset */
    if (area.w < PV_MIN_W || area.h < PV_MIN_H) return -1;
    memset(v, 0, sizeof *v);
    v->tk = tk;
    v->ctl = ctl;
    v->lang = (unsigned)lang < CTL_LANG_COUNT ? lang : CTL_LANG_EN;

    /* One layout pass up front: widget creation and every later render use the same geometry. */
    v->area = area;
    layout_build(v, area);

    int ok = 1;
    v->root = mk(v, tk->view_create, parent, area.x, area.y, area.w, area.h, "peq_view", &ok);
    if (ok) v->phase = PV_OPEN; /* teardown() now owns cleanup */
    v->root_destroy_id = on(v, v->root, tk->evt_destroy, on_root_destroy, v, &ok);
    v->toggle_btn = mk(v, tk->button_create, v->root, v->lay.toggle_x, v->lay.toggle_y, v->lay.toggle_w,
                       v->lay.toggle_h, "peq_toggle", &ok);
    v->selected_label = mk(v, tk->label_create, v->root, v->lay.content_x, v->lay.content_y,
                           v->lay.content_w, v->lay.sel_h, "peq_selected", &ok);
    v->status_label = mk(v, tk->label_create, v->root, v->lay.content_x,
                         v->lay.content_y + v->lay.sel_h, v->lay.content_w, v->lay.status_h,
                         "peq_status", &ok);
    v->list = mk(v, tk->scroll_view_create, v->root, v->lay.list_x, v->lay.list_y, v->lay.list_w,
                 v->lay.list_h, "peq_list", &ok);
    for (int i = 0; i < CTL_MAX_ENTRIES && ok; ++i) {
        char name[16];
        snprintf(name, sizeof name, "peq_row_%d", i);
        v->rows[i] = mk(v, tk->button_create, v->list, 0, (pv_xy)(i * v->row_h), v->lay.list_w,
                        v->row_h - 4, name, &ok);
        v->tokens[i].view = v;
        v->tokens[i].row = i;
        v->row_click_id[i] = on(v, v->rows[i], tk->evt_click, on_row_click, &v->tokens[i], &ok);
    }
    v->rescan_btn = mk(v, tk->button_create, v->root, v->lay.rescan_x, v->lay.rescan_y, v->lay.rescan_w,
                       v->lay.rescan_h, "peq_rescan", &ok);
    /* Disabled pane container plus three interactive surfaces: plain view/label widgets do not
     * necessarily emit EVT_CLICK on a pointer tap, so the material and both texts are buttons. */
    v->off_pane = mk(v, tk->view_create, v->root, 0, v->lay.off_y, v->lay.off_pane_w, v->lay.off_h,
                     "peq_off_pane", &ok);
    v->off_icon = mk(v, tk->button_create, v->off_pane, (v->lay.off_pane_w - PV_OFF_ICON) / 2, 0,
                     PV_OFF_ICON, v->lay.off_icon_h, "peq_off_icon", &ok);
    v->off_label = mk(v, tk->button_create, v->off_pane, 0, v->lay.off_icon_h + v->lay.off_gap,
                      v->lay.off_pane_w, v->lay.off_label_h, "peq_off_label", &ok);
    v->off_note = mk(v, tk->button_create, v->off_pane, 0,
                     PV_OFF_NOTE_Y - PV_OFF_ICON_Y,
                     v->lay.off_pane_w, v->lay.off_note_h, "peq_off_note", &ok);
    v->toggle_click_id = on(v, v->toggle_btn, tk->evt_click, on_toggle_click, v, &ok);
    v->rescan_click_id = on(v, v->rescan_btn, tk->evt_click, on_rescan_click, v, &ok);
    v->off_click_id[0] = on(v, v->off_icon, tk->evt_click, on_off_click, v, &ok);
    v->off_click_id[1] = on(v, v->off_label, tk->evt_click, on_off_click, v, &ok);
    v->off_click_id[2] = on(v, v->off_note, tk->evt_click, on_off_click, v, &ok);
    if (!ok) {
        teardown(v);
        return -1;
    }
    v->rows_shown = -1; /* force list geometry on the first render */
    ctl_open(ctl);
    render(v);
    return 0;
}
