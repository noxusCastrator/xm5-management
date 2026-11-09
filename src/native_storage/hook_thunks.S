.set noreorder
.option pic0
.text
.macro THUNK name,addr
.globl \name
\name:
 lui $25,(\addr >> 16)&0xffff
 ori $25,$25,\addr&0xffff
 /* Import stubs use the executable GOT. A shared-library callee can return
  * with its own gp; freestanding callers have no automatic cprestore. */
 lui $28,0x80
 ori $28,$28,0x54d0
 jr $25
 nop
.endm
THUNK stock_tfstatus,0x4dd4a0
THUNK stock_play_status,0x5ccbac
THUNK stock_sqlite_exec,0x746710
THUNK stock_sqlite_open,0x746f50
THUNK stock_sqlite_close,0x7483c0
THUNK stock_file_access,0x746d10
.globl pa_call_foreign_close
pa_call_foreign_close:
 move $25,$5
 lui $28,0x80
 ori $28,$28,0x54d0
 jr $25
 nop
THUNK stock_asset_create,0x6f40d4
THUNK stock_asset_destroy,0x6f4220
THUNK stock_album_cover,0x5e57d0
THUNK stock_lookup,0x661804
THUNK stock_deque_size,0x747f60
THUNK stock_deque_at,0x747640
THUNK stock_load_image,0x665e68
THUNK stock_unload_image,0x666618
THUNK stock_draw_type,0x628544
THUNK stock_destroy_children,0x66b29c
THUNK stock_get_image_prop,0x663370
THUNK stock_folder_timer,0x53ab90
THUNK stock_mutex_lock,0x746950
THUNK stock_mutex_unlock,0x747e40
THUNK stock_image_add,0x6503c8
THUNK stock_image_decode,0x6500c4
THUNK stock_bitmap_size,0x63d9c4
THUNK stock_bitmap_destroy,0x63ab44
THUNK stock_calloc,0x7064b4
THUNK stock_strdup,0x713e84
THUNK stock_free,0x706604
THUNK stock_time,0x711910
THUNK stock_array_push,0x6f5fdc
THUNK stock_asset_unref,0x639e74
/* Verified caller locals at 0x6508c4: s4=name, s2=manager. */
.globl pa_decode_entry
pa_decode_entry:
 move $6,$20
 move $7,$18
 j pa_decode
 nop
/* Resume original PIC routines after the two overwritten gp setup words. */
.macro ORIGINAL name,addr,hi,lo
.globl \name
\name:
 lui $25,(\addr >> 16)&0xffff
 ori $25,$25,\addr&0xffff
 lui $28,\hi
 addiu $28,$28,\lo
 j \addr+8
 nop
.endm
#include "extra_thunks.inc"
ORIGINAL stock_set_cover,0x4d7ae4,0x33,-9748
ORIGINAL stock_set_eq,0x5cc094,0x24,-27588
ORIGINAL stock_loader_load,0x69e09c,0x16,29748
ORIGINAL stock_loader_exist,0x69e084,0x16,29772
ORIGINAL stock_folder_row,0x53a81c,0x2d,-21324
ORIGINAL stock_folder_play,0x53d548,0x2c,32648
ORIGINAL stock_jump_play,0x545f10,0x2c,-2624
ORIGINAL stock_power_play,0x54ad18,0x2c,-22600

THUNK stock_widget_image_manager,0x665d84
THUNK stock_need_transform,0x64fcfc
THUNK stock_canvas_image_ex,0x643ec4
THUNK stock_canvas_image,0x640364
THUNK stock_owner_drop,0x6501cc
THUNK stock_timer_add,0x65e2c0
THUNK stock_idle_add,0x64ee30
THUNK stock_graphic_data,0x68de00
THUNK stock_asset_ref,0x6f4338
THUNK stock_asset_info_unref,0x6f42c8
THUNK stock_bitmap_line,0x63ad60
ORIGINAL stock_set_image,0x64f6a0,0x1b,24112
ORIGINAL stock_image_destroy,0x64f5dc,0x1b,24308
ORIGINAL stock_manager_destroy,0x650e38,0x1b,18072
ORIGINAL stock_image_paint,0x628074,0x1e,-11172


THUNK stock_snprintf,0x747480
THUNK stock_list_m3u,0x5e4084
THUNK stock_system,0x7480b0
ORIGINAL stock_save_playlist,0x4ed448,0x32,-32632
.globl stock_nf_init
stock_nf_init:
 .word 0x27bdffe0
 .word 0x8fac0030
 j 0x824770
 nop

THUNK stock_parse_playlist,0x5e4328
THUNK stock_deque_erase,0x748170
THUNK stock_deque_begin,0x746b50
THUNK stock_iterator_next,0x746c80
THUNK stock_deque_clear,0x7479a0
THUNK stock_std_strdup,0x748230
.globl stock_std_free
stock_std_free:
 lui $28,0x80
 ori $28,$28,0x54d0
 lw $25,-3544($28)
 jr $25
 nop
.globl pa_parse_playlist_entry
pa_parse_playlist_entry:
 move $5,$21
 j pa_parse_playlist
 nop
