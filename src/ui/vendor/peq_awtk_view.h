/* AWTK touch view for the portable PEQ preset controller (U2 controller.h / view.h).
 *
 * The view never calls AWTK directly: every toolkit operation goes through pv_toolkit, a typed
 * function table filled in by the page owner. No addresses, no native bindings live here.
 *
 * Build modes
 *   default          opaque host types (struct _widget_t etc.), used by the fake-toolkit tests.
 *   -DPV_WITH_AWTK   include "awtk.h" and use the real widget_t/event_t/idle_info_t/ret_t/bool_t/
 *                    xy_t/wh_t, so AWTK functions whose prototypes match can be stored in the table
 *                    without casts (see README "Function table").
 *
 * Threading: UI thread only. No allocation, no timers, no background work, no file access: the only
 * deferred work is one idle callback per user/owner request, owned and cancelled by the view.
 *
 * Layout contract (owner passes its content area as pv_rect; px)
 *   - The top PV_HEADER_H (82) rows stay inside the passed area and are the header: the PEQ toggle
 *     sits at x = area.w - PV_TOGGLE_RIGHT_GAP - PV_TOGGLE_W (= area.w - 228 => 492 at the stock 720
 *     width), h = 82. The owner's binder may add back/title widgets in the same strip and the
 *     rightmost PV_RIGHT_PLAY_RESERVE (108) columns of the HEADER are reserved for the native
 *     animated playing button: no header widget of this view reaches into them.
 *   - Below the header nothing is reserved: list, rescan and the disabled pane use the full area
 *     width with symmetric PV_SIDE (30) px side margins.
 *   - Everything is fully contained in `area`:
 *       enabled pane : selected preset, compact status, a scroll list whose viewport is exactly
 *                      PV_LIST_ROWS (4) complete rows, and a full-width rescan button with at least
 *                      PV_BOTTOM_MARGIN (16) px below it.
 *       browse pane  : the same list UI shown while the controller is OFF (view-only "browse while
 *                      off": the off pane is the entry point, and browsing or selecting never
 *                      enables the controller).
 *       disabled pane: stock material placeholder (120x120, centered at x = (area.w-120)/2) plus
 *                      PEQ OFF and the 28 px localized explanation centered across the full width.
 *   - Row height is PV_ROW_H (80) whenever the area height allows it and shrinks
 *     (PV_MIN_ROW_H..PV_ROW_H) only for the shortest accepted areas; the list viewport is always
 *     exactly 3 complete rows at the row height actually used (pv_list_viewport_h()).
 *   - Every render re-asserts all 64 row rectangles through pv_toolkit.widget_move_resize: a visible
 *     row is (0, i*row_h, list_w, row_h-4) and a hidden row is (0, 0, list_w, 0). AWTK's
 *     scroll_view_on_layout_children recomputes the virtual size from child geometry including
 *     invisible children, so zeroing hidden rows is what keeps the 64 reusable slots from creating
 *     a scroll range through rows that are not there. widget_move_resize is required.
 *   - No text is measured or truncated here: labels are native hscroll_labels and receive the
 *     original UTF-8 text (bounded only by the caller's buffers), so native rendering/scrolling
 *     decides how it is shown. The selected preset name is handed to the controller in full.
 *   - The technical coverage row and the redundant on/off row do not exist; the localized
 *     explanation is shown only on the plain disabled pane. A success status line that merely
 *     echoes the selected preset name is suppressed (errors and warnings always stay visible). */
#ifndef PEQ_AWTK_VIEW_H
#define PEQ_AWTK_VIEW_H

#include <stdint.h>

#include "controller.h"
#include "view.h"

#ifdef PV_WITH_AWTK
#include "awtk.h"
typedef widget_t pv_widget;
typedef event_t pv_event;
typedef idle_info_t pv_idle_info;
typedef ret_t pv_ret;
typedef bool_t pv_bool;
typedef xy_t pv_xy;
typedef wh_t pv_wh;
#define PV_RET_OK RET_OK
#define PV_RET_REMOVE RET_REMOVE
#else
typedef struct _widget_t pv_widget;
typedef struct _event_t pv_event;
/* Host stand-in for AWTK idle_info_t: the view reads only the public `ctx` member. */
typedef struct _idle_info_t { void *ctx; uint32_t id; } pv_idle_info;
typedef enum { PV_RET_OK = 0, PV_RET_REMOVE = 1 } pv_ret; /* host values; AWTK mode uses RET_* */
typedef _Bool pv_bool;
typedef int32_t pv_xy;
typedef int32_t pv_wh;
#endif

/* Same shape as AWTK event_func_t: ret_t (*)(void* ctx, event_t* e). */
typedef pv_ret (*pv_event_fn)(void *ctx, pv_event *e);
/* Same shape as AWTK idle_func_t: ret_t (*)(const idle_info_t* idle). */
typedef pv_ret (*pv_idle_fn)(const pv_idle_info *info);

typedef pv_widget *(*pv_create_fn)(pv_widget *parent, pv_xy x, pv_xy y, pv_wh w, pv_wh h);
/* Same shape as AWTK widget_move_resize(widget_t*, xy_t, xy_t, wh_t, wh_t). */
typedef pv_ret (*pv_move_resize_fn)(pv_widget *widget, pv_xy x, pv_xy y, pv_wh w, pv_wh h);

/* Filled by the view at the end of every layout pass so a host/page owner can check the geometry
 * without any toolkit dependency. All values are inside the pv_rect handed to pv_open. */
typedef struct {
    pv_xy header_y, header_h;        /* header strip: y = area.y, h = PV_HEADER_H */
    pv_xy toggle_x, toggle_y, toggle_w, toggle_h;
    pv_wh reset_x;                   /* first x right of the header playing-button reserve */
    pv_xy content_x, content_w;      /* below-header content column (symmetric side margins) */
    pv_xy content_y, sel_h, status_h;/* content top and the selected/status row heights */
    pv_xy list_x, list_y, list_w, list_h; /* list viewport: list_h == row_h * PV_LIST_ROWS exactly */
    pv_wh row_h;
    pv_xy rescan_x, rescan_y, rescan_w, rescan_h;
    pv_xy bottom_margin;             /* area bottom - (rescan_y + rescan_h), >= PV_BOTTOM_MARGIN */
    pv_xy off_x, off_y, off_w, off_h;/* disabled pane material (off_x = centered material x) */
    pv_wh off_pane_w;                /* disabled pane spans the full owner width */
    pv_xy off_gap;                   /* gap between material, PEQ OFF and the note */
    pv_xy off_icon_h, off_label_h, off_note_h; /* heights inside the disabled pane */
} pv_layout_metrics;

typedef struct pv_toolkit {
    /* Widget construction, all AWTK "xxx_create(parent, x, y, w, h)"; NULL on failure. */
    pv_create_fn view_create;
    pv_create_fn label_create;
    pv_create_fn button_create;
    pv_create_fn scroll_view_create;
    pv_ret (*scroll_view_set_virtual_h)(pv_widget *w, pv_wh h);
    pv_ret (*scroll_view_set_offset)(pv_widget *w, int32_t xoffset, int32_t yoffset);
    /* Required: AWTK widget_move_resize. Used to reset every row rectangle on every render. */
    pv_move_resize_fn widget_move_resize;
    /* Properties. set_text_utf8 must copy the string (AWTK does): callers pass stack buffers. */
    pv_ret (*widget_set_name)(pv_widget *w, const char *name);
    pv_ret (*widget_set_text_utf8)(pv_widget *w, const char *text);
    pv_ret (*widget_set_visible)(pv_widget *w, pv_bool visible);
    pv_ret (*widget_set_enable)(pv_widget *w, pv_bool enable);
    pv_ret (*widget_set_style_color)(pv_widget *w, const char *state_and_name, uint32_t value);
    /* Events: widget_on returns a registration id, 0 on failure. */
    uint32_t (*widget_on)(pv_widget *w, uint32_t type, pv_event_fn on_event, void *ctx);
    pv_ret (*widget_off)(pv_widget *w, uint32_t id);
    pv_ret (*widget_destroy)(pv_widget *w);
    /* Main-loop idle (AWTK idle_add/idle_remove): id, 0 on failure. The callback reads info->ctx. */
    uint32_t (*idle_add)(pv_idle_fn on_idle, void *ctx);
    pv_ret (*idle_remove)(uint32_t id);
    /* Enum facts taken from the target's headers, not from this file. */
    uint32_t evt_click;   /* AWTK EVT_CLICK */
    uint32_t evt_destroy; /* AWTK EVT_DESTROY */
    /* Style values passed to widget_set_style_color (toolkit colour encoding). */
    uint32_t text_normal, text_error, bg_row, bg_row_selected;
    /* Optional stock material binding. Host lifecycle toolkit may leave NULL. */
    void (*decorate)(pv_widget *, const char *role);
    void (*toggle_image)(pv_widget *, int on);
    /* Optional geometry probe (may be NULL): called after every layout pass. */
    void (*layout_probe)(const pv_layout_metrics *metrics, void *ctx);
    void *layout_probe_ctx;
} pv_toolkit;

typedef struct { pv_xy x, y; pv_wh w, h; } pv_rect;

/* Content layout inside the owner's area (pixels). Minimum area height is PV_MIN_H. */
#define PV_PAD 16                   /* legacy edge pad kept for older callers */
#define PV_SIDE 30                  /* symmetric side margin below the header */
#define PV_ROW_H 80                 /* preferred row height */
#define PV_MIN_ROW_H 56             /* smallest row height used on the shortest accepted area */
#define PV_LIST_ROWS 4              /* complete rows the list viewport always shows */
#define PV_HEADER_H 82              /* header strip at the top of the owner's area */
#define PV_TOGGLE_W 104
#define PV_RIGHT_PLAY_RESERVE 108   /* rightmost HEADER columns reserved for the native playing button */
#define PV_TOGGLE_RIGHT_GAP 124     /* toggle right edge to area right: keeps x = area.w-228 and the
                                     * 16 px clearance to the header playing-button reserve */
#define PV_OFF_ICON_Y 158
#define PV_OFF_LABEL_Y 306
#define PV_OFF_NOTE_Y 355
#define PV_OFF_ICON 120             /* disabled pane material */
#define PV_OFF_LABEL_H 32
#define PV_OFF_NOTE_H 28
#define PV_OFF_GAP 8
#define PV_BOTTOM_MARGIN 16         /* minimum gap below the rescan button */
#define PV_LIST_GAP 12              /* gap between the list viewport and the rescan button */
#define PV_MIN_W 480
#define PV_MIN_H 500
#define PV_SELECTED_MARK "> "
/* Disabled pane: PEQ OFF is universal (same text in every locale), the explanation is localized. */
#define PV_OFF_LABEL "PEQ OFF"
#define PV_OFF_NOTE_EN "Local music up to 192kHz"
#define PV_OFF_NOTE_ZH "PEQ对本地音乐含192kHz以下有效"

/* Compatibility macro: V9 keeps this only so that pre-V9 callers keep compiling. U7's technical
 * coverage row is not displayed by the revised layout (see peq_awtk_view.c). */
#define PV_COVERAGE_EN "PCM <=192k; stock <=48k; DSD bypass"
#define PV_COVERAGE_ZH "PCM ≤192k；十段 ≤48k；DSD 旁通"

typedef enum { PV_CLOSED = 0, PV_OPEN, PV_DETACHED } pv_phase;
/* Deferred user/owner requests. BROWSE is view-only: it shows the list UI while the controller
 * stays OFF and never calls ctl_set_enabled(). */
typedef enum { PV_ACT_NONE = 0, PV_ACT_RESCAN, PV_ACT_SELECT, PV_ACT_TOGGLE, PV_ACT_BROWSE } pv_action;
/* Which pane the last render showed.
 * PV_PANE_NONE until the first render; PV_PANE_OFF (plain stock-like disabled pane),
 * PV_PANE_BROWSE (list UI while the controller is OFF) or PV_PANE_ON (controller enabled). */
typedef enum { PV_PANE_NONE = 0, PV_PANE_ON, PV_PANE_OFF, PV_PANE_BROWSE } pv_pane;

struct pv_view;
/* Per-row callback context. Lives in pv_view (not in a widget) so it never outlives the view. */
typedef struct {
    struct pv_view *view;
    int row;
    unsigned generation; /* controller list generation this row was last rendered with */
} pv_row_token;

typedef struct pv_view {
    const pv_toolkit *tk;    /* must outlive the open view (the binder passes a static table) */
    ctl_controller *ctl;     /* owned by the caller; must outlive the open view */
    ctl_lang lang;
    pv_phase phase;
    pv_pane showing_pane;    /* pane the widgets are currently set up for */
    int pane_valid;          /* 0 until the first render applied pane visibility */
    int browse;              /* view-only browse-while-off: list UI while the controller stays OFF */
    pv_widget *root, *selected_label, *toggle_btn, *status_label;
    pv_widget *list, *rescan_btn;
    pv_widget *off_pane, *off_icon, *off_label, *off_note;
    pv_widget *rows[CTL_MAX_ENTRIES];
    pv_row_token tokens[CTL_MAX_ENTRIES];
    uint32_t row_click_id[CTL_MAX_ENTRIES], toggle_click_id, rescan_click_id, root_destroy_id;
    uint32_t off_click_id[3]; /* off material, PEQ OFF, explanation: each queues BROWSE */
    pv_wh row_h;             /* row height actually used (PV_MIN_ROW_H..PV_ROW_H);
                              * the list viewport is row_h * PV_LIST_ROWS */
    pv_layout_metrics lay;   /* geometry of the last layout pass (containment checks) */
    pv_rect area;            /* owner area this view was opened with */
    /* Deferred work: at most one idle; at most one pending user action plus owner flags. */
    uint32_t idle_id;
    pv_action pending;
    unsigned pending_generation;
    int pending_row;
    int pending_reopen;
    /* What is on screen. */
    int rows_shown;
    unsigned rows_generation;
    pv_pane last_pane_geometry; /* pane the row rectangles were last built for */
    unsigned renders;        /* diagnostic counter */
} pv_view;

/* Build the page content as one child view of `parent` covering `area` (the owner's content area,
 * header strip included), call ctl_open() and render synchronously. The owner's navigation bar,
 * title, switch and back button are not touched: this view creates no back control and does not
 * navigate. `tk->widget_move_resize` is required (row geometry reset).
 * Returns 0, or -1 (bad arguments, area smaller than PV_MIN_W x PV_MIN_H, already open, missing
 * required callback, or a toolkit failure) after removing everything it created. Do not call from
 * an event dispatched to a widget of this view. */
int pv_open(pv_view *v, const pv_toolkit *tk, ctl_controller *ctl, pv_widget *parent, pv_rect area,
            ctl_lang lang);

/* Cancel the pending idle, unregister every handler and destroy the view's root synchronously.
 * Safe to call repeatedly, after the owner already destroyed the parent (PV_DETACHED), and from
 * the owner's own event handlers (e.g. the navigation bar back button) because the event target
 * is then outside this view. */
void pv_close(pv_view *v);

/* Owner navigation-bar back: pv_close() then ctl_back() (platform navigate_back, i.e. the owner's
 * existing window close). The view itself has no back control. */
void pv_back(pv_view *v);

/* Explicit owner refresh, deferred to one idle. reopen=0 re-renders the current model;
 * reopen=1 calls ctl_open() first (re-reads on/off owned by the stock EQ flag, the stored preset
 * and the directory), e.g. after the owner's navigation-bar switch changed the EQ flag. */
void pv_refresh(pv_view *v, int reopen);

/* Change label language; deferred re-render. */
void pv_set_lang(pv_view *v, ctl_lang lang);

/* Layout of the last pass, or NULL when the view never opened. Read-only, no toolkit access. */
const pv_layout_metrics *pv_layout(const pv_view *v);
/* Row height / list viewport height of the last layout pass: list viewport height is always exactly
 * PV_LIST_ROWS complete rows (pv_list_viewport_h() == row height * 4). */
pv_wh pv_row_height(const pv_view *v);
pv_wh pv_list_viewport_h(const pv_view *v);
/* Pane the widgets are currently set up for (PV_PANE_NONE before the first render of an open view). */
pv_pane pv_showing_pane(const pv_view *v);
/* Non-zero while the controller-enabled list pane is showing. */
int pv_pane_enabled(const pv_view *v);
/* Non-zero while the view-only browse-while-off list pane is showing (controller is OFF). */
int pv_browse_active(const pv_view *v);

/* Localized disabled-pane strings. pv_off_label() is locale independent ("PEQ OFF");
 * pv_off_note_text() is the 28 px explanation shown only while PEQ is off. */
const char *pv_off_label(void);
const char *pv_off_note_text(ctl_lang lang);

#endif
