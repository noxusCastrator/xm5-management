# >>> xm5-rx3-hook begin
# RX3 TF-card rescue hook, run before demo. Does nothing unless the mounted TF
# card root holds exactly one regular trigger file:
#   XM5_DUMP    read-only dump of non-private mtd partitions into a new run dir
#   XM5_RESCUE  publish the pinned official V2.43 archive at card root, then
#               set ota:kernel2 on mtd6 (stock demo's commands) and reboot
# Environment variables are never consulted; paths and limits are fixed below.
# Each triggered run writes XM5RX3-run.*/log.txt and RESULT on the card and
# appends one line to XM5RX3.log. Lines tagged #G: are guards checked by the
# RX3 mutation tests.

xr_log() {
  echo "$*" >>"$XR_LOG" 2>/dev/null || echo "xm5rx3: $*" >&2
}

xr_result() {
  XR_RESULT=$1
  [ -n "$XR_RUN" ] && echo "$1" >"$XR_RUN/RESULT" 2>/dev/null
  return 0
}

xr_fail() {
  xr_log "FAIL $1: $2"
  xr_result "$1"
}

# run "$@" for at most $1 seconds (BusyBox here has no timeout applet);
# returns the command status, or 124 if it was killed
xr_bounded() {
  xr_bt=$1; shift
  "$@" </dev/null &
  xr_bp=$!
  xr_bi=0
  while kill -0 "$xr_bp" 2>/dev/null; do
    if [ "$xr_bi" -ge "$xr_bt" ]; then
      kill "$xr_bp" 2>/dev/null; sleep 1; kill -9 "$xr_bp" 2>/dev/null
      wait "$xr_bp" 2>/dev/null
      return 124
    fi
    sleep 1
    xr_bi=$((xr_bi + 1))
  done
  wait "$xr_bp"
}

xr_size() {
  wc -c <"$1" 2>/dev/null | tr -d ' '
}

xr_free_kb() {
  xr_fk=$(df -P -k "$XR_M" 2>/dev/null | awk 'NR == 2 { print $4 }')
  case $xr_fk in ''|*[!0-9]*) echo -1 ;; *) echo "$xr_fk" ;; esac
}

# FILE SIZE SHA256: regular file (not symlink) with exactly this content
xr_verify_file() {
  if [ ! -f "$1" ] || [ -L "$1" ]; then xr_log "verify: $1 missing or not a regular file"; return 1; fi
  xr_vs=$(xr_size "$1")
  [ "$xr_vs" = "$2" ] || { xr_log "verify: $1 size $xr_vs != $2"; return 1; }
  xr_vh=$(xr_bounded "$XR_T_HASH" sha256sum "$1" 2>>"$XR_LOG") || { xr_log "verify: sha256sum $1 failed"; return 1; }
  [ "${xr_vh%% *}" = "$3" ] || { xr_log "verify: $1 sha256 ${xr_vh%% *} != $3"; return 1; }
}

# DIR/recovery-update is a real directory holding exactly xImage and rootfs.squashfs
xr_rudir_ok() {
  [ -d "$1/recovery-update" ] && [ ! -L "$1/recovery-update" ] || return 1
  [ "$(ls -A "$1/recovery-update" 2>/dev/null | wc -l | tr -d ' ')" = 2 ]
}

xr_files_ok() {
  xr_verify_file "$1/firmware_v20.info" "$XR_I_SIZE" "$XR_I_SHA" &&
  xr_verify_file "$1/recovery-update/xImage" "$XR_K_SIZE" "$XR_K_SHA" &&
  xr_verify_file "$1/recovery-update/rootfs.squashfs" "$XR_R_SIZE" "$XR_R_SHA"
}

# 0 if the card root has neither recovery publication name (any type)
xr_no_conflict() {
  for xr_c in firmware_v20.info recovery-update; do
    if [ -e "$XR_M/$xr_c" ] || [ -L "$XR_M/$xr_c" ]; then
      xr_log "card root already has $xr_c; it is never reused or overwritten (move it off the card root and retry)"
      return 1
    fi
  done
}

# mtd6 must be the single partition named "ota", a sane size, and a char device
xr_ota_layout() {
  [ "$(grep -c '"ota"$' "$XR_PROC/mtd" 2>/dev/null)" = 1 ] || { xr_log "mtd: need exactly one partition named \"ota\""; return 1; }
  xr_ol=$(grep '^mtd6: ' "$XR_PROC/mtd" 2>/dev/null)
  set -- $xr_ol
  if [ "$#" -ne 4 ] || [ "$1" != mtd6: ] || [ "$4" != '"ota"' ]; then xr_log "mtd: mtd6 is not \"ota\": $xr_ol"; return 1; fi
  case "$2$3" in *[!0-9a-f]*) xr_log "mtd: unparsable mtd6 line: $xr_ol"; return 1 ;; esac
  [ "${#2}" -eq 8 ] && [ "${#3}" -eq 8 ] || { xr_log "mtd: unparsable mtd6 line: $xr_ol"; return 1; }
  XR_OTA_SIZE=$((0x$2)); XR_OTA_ERASE=$((0x$3))
  if [ "$XR_OTA_ERASE" -lt 2048 ] || [ "$XR_OTA_SIZE" -lt "$XR_OTA_ERASE" ] || [ $((XR_OTA_SIZE % XR_OTA_ERASE)) -ne 0 ]; then
    xr_log "mtd: implausible mtd6 size $XR_OTA_SIZE / erase $XR_OTA_ERASE"; return 1
  fi
  [ -c "$XR_DEV/mtd6" ] || { xr_log "mtd: $XR_DEV/mtd6 is not a character device"; return 1; }
}

# move trigger NAME into the run dir; succeed only if that is durably done
xr_consume() {
  mv "$XR_M/$1" "$XR_RUN/$1" >>"$XR_LOG" 2>&1
  xr_rc=$?
  [ "$xr_rc" -eq 0 ] || { xr_log "consume: mv $1 failed rc=$xr_rc"; return 1; }   #G:consume_rc
  { [ ! -e "$XR_M/$1" ] && [ ! -L "$XR_M/$1" ] && [ -f "$XR_RUN/$1" ] && [ ! -L "$XR_RUN/$1" ]; } || { xr_log "consume: $1 not moved as expected"; return 1; }   #G:consume_verify
  sync || { xr_log "consume: sync failed after moving $1"; return 1; }   #G:consume_sync
  xr_log "consumed $1 -> ${XR_RUN##*/}/$1"
}

# move back whatever this run published at card root into its staging dir
xr_retract() {
  case $xr_pub in
    *f) mv "$XR_M/firmware_v20.info" "$XR_STAGE/firmware_v20.info" >>"$XR_LOG" 2>&1 ||
        xr_log "RETRACT FAILED: card-root firmware_v20.info from this run is still published; remove it manually" ;;
  esac
  case $xr_pub in
    d*) mv "$XR_M/recovery-update" "$XR_STAGE/recovery-update" >>"$XR_LOG" 2>&1 ||
        xr_log "RETRACT FAILED: card-root recovery-update from this run is still published; remove it manually" ;;
  esac
  [ -n "$xr_pub" ] && xr_log "retracted publication ($xr_pub)"
  xr_pub=
  sync
}

# remove only the three known files and two dirs this run created; never recursive
xr_stage_cleanup() {
  [ -n "$XR_STAGE" ] || return 0
  rm -f "$XR_STAGE/firmware_v20.info"
  if [ -d "$XR_STAGE/recovery-update" ] && [ ! -L "$XR_STAGE/recovery-update" ]; then
    rm -f "$XR_STAGE/recovery-update/xImage" "$XR_STAGE/recovery-update/rootfs.squashfs"
    rmdir "$XR_STAGE/recovery-update" 2>/dev/null
  fi
  if rmdir "$XR_STAGE" 2>/dev/null; then
    xr_log "staging ${XR_STAGE##*/} removed"
  else
    xr_log "staging ${XR_STAGE##*/} left on card (unexpected entries); inspect and remove manually"
  fi
  XR_STAGE=
  sync
}

xr_dump() {
  xr_d=$XR_RUN/dump
  mkdir "$xr_d" 2>>"$XR_LOG" || { xr_fail DUMP_INCOMPLETE "cannot create dump dir"; return 1; }
  for xr_p in mtd partitions version cmdline; do
    cat "$XR_PROC/$xr_p" >"$xr_d/proc_$xr_p.txt" 2>>"$XR_LOG" || { xr_fail DUMP_INCOMPLETE "cannot copy $XR_PROC/$xr_p"; return 1; }
  done
  : >"$xr_d/plan.txt" && : >"$xr_d/skipped.txt" || { xr_fail DUMP_INCOMPLETE "cannot write plan"; return 1; }
  xr_total=0
  while read -r xr_dev xr_hsize xr_herase xr_name; do
    case $xr_dev in mtd[0-9]*:) ;; *) continue ;; esac
    xr_n=${xr_dev#mtd}; xr_n=${xr_n%:}
    xr_name=${xr_name#\"}; xr_name=${xr_name%\"}
    case $xr_n$xr_hsize in *[!0-9a-f]*|'') xr_fail DUMP_INCOMPLETE "unparsable $XR_PROC/mtd line for $xr_dev"; return 1 ;; esac
    xr_lc=$(echo "$xr_name" | tr 'A-Z' 'a-z')
    case $xr_lc in
      ''|*[!a-z0-9_.-]*) echo "skipped mtd$xr_n unusual-name" >>"$xr_d/skipped.txt"; continue ;;
      *data*|*user*|*mac*|*serial*|*key*|*secret*|*cred*) echo "skipped mtd$xr_n $xr_name private" >>"$xr_d/skipped.txt"; continue ;;
    esac
    echo "$xr_n $((0x$xr_hsize)) $xr_name" >>"$xr_d/plan.txt" || { xr_fail DUMP_INCOMPLETE "cannot write plan"; return 1; }
    xr_total=$((xr_total + 0x$xr_hsize))
  done <"$xr_d/proc_mtd.txt"
  [ -s "$xr_d/plan.txt" ] || { xr_fail DUMP_INCOMPLETE "no dumpable partitions in $XR_PROC/mtd"; return 1; }
  xr_need=$((xr_total / 1024 + XR_MARGIN_KB)); xr_free=$(xr_free_kb)
  [ "$xr_free" -ge "$xr_need" ] || { xr_fail DUMP_INCOMPLETE "need ${xr_need}KiB free, have ${xr_free}KiB; nothing dumped"; return 1; }   #G:dump_space
  : >"$xr_d/MANIFEST.partial" || { xr_fail DUMP_INCOMPLETE "cannot write manifest"; return 1; }
  while read -r xr_n xr_size xr_name; do
    xr_f=$xr_d/mtd$xr_n-$xr_name.bin
    xr_t=$((XR_T_DUMP_BASE + xr_size / 262144))
    xr_log "nanddump mtd$xr_n ($xr_name, $xr_size bytes, limit ${xr_t}s)"
    xr_bounded "$xr_t" "$XR_NANDDUMP" --bb=padbad -f "$xr_f" "$XR_DEV/mtd$xr_n" >"$xr_d/mtd$xr_n.nanddump.log" 2>&1
    xr_rc=$?
    [ "$xr_rc" -eq 0 ] || { xr_fail DUMP_INCOMPLETE "nanddump mtd$xr_n rc=$xr_rc (124 = timed out)"; return 1; }   #G:dump_rc
    { [ -f "$xr_f" ] && [ ! -L "$xr_f" ] && [ "$(xr_size "$xr_f")" = "$xr_size" ]; } || { xr_fail DUMP_INCOMPLETE "mtd$xr_n image is not $xr_size bytes"; return 1; }   #G:dump_size
    xr_h=$(xr_bounded "$XR_T_HASH" sha256sum "$xr_f" 2>>"$XR_LOG") || { xr_fail DUMP_INCOMPLETE "sha256sum mtd$xr_n failed"; return 1; }
    { echo "part mtd$xr_n $xr_name $xr_size ${xr_h%% *}" &&
      grep -E 'ECC|[Bb]ad block' "$xr_d/mtd$xr_n.nanddump.log" | sed "s/^/note mtd$xr_n /"; } >>"$xr_d/MANIFEST.partial" ||
      { xr_fail DUMP_INCOMPLETE "cannot write manifest"; return 1; }
  done <"$xr_d/plan.txt"
  { echo "xm5rx3-dump-manifest 1"
    echo "status complete"
    echo "reader nanddump --bb=padbad: ECC-corrected page data, OOB omitted, bad blocks replaced by 0xFF"
    echo "restore_ready no"
    cat "$xr_d/MANIFEST.partial" "$xr_d/skipped.txt"; } >"$xr_d/MANIFEST.tmp" &&
    sync && mv "$xr_d/MANIFEST.tmp" "$xr_d/MANIFEST.complete" && sync ||
    { xr_fail DUMP_INCOMPLETE "cannot finalize manifest"; return 1; }
  rm -f "$xr_d/MANIFEST.partial"
  xr_log "dump complete: $(wc -l <"$xr_d/plan.txt" | tr -d ' ') partitions"
  xr_result DUMP_COMPLETE
}

xr_rescue() {
  xr_a=$XR_M/$XR_A_NAME
  xr_pub=; XR_STAGE=
  xr_log "rescue: only $XR_A_NAME with sha256 $XR_A_SHA is accepted"
  if [ ! -f "$xr_a" ] || [ -L "$xr_a" ]; then xr_fail ARCHIVE_MISSING "$XR_A_NAME absent or not a regular file"; return 1; fi
  xr_s=$(xr_size "$xr_a")
  [ "$xr_s" = "$XR_A_SIZE" ] || { xr_fail ARCHIVE_SIZE "$XR_A_NAME is $xr_s bytes, pinned archive is $XR_A_SIZE"; return 1; }
  xr_no_conflict || { xr_fail PUBLISH_CONFLICT "stale recovery files at card root; nothing extracted"; return 1; }   #G:conflict_pre
  xr_ota_layout || { xr_fail MTD_LAYOUT "ota partition check failed; nothing extracted"; return 1; }   #G:mtd_layout
  xr_need=$(( (XR_A_UNPACKED + XR_OTA_SIZE) / 1024 + XR_MARGIN_KB )); xr_free=$(xr_free_kb)
  [ "$xr_free" -ge "$xr_need" ] || { xr_fail NO_SPACE "need ${xr_need}KiB free, have ${xr_free}KiB"; return 1; }   #G:space

  xr_log "hashing $XR_A_NAME (limit ${XR_T_HASH}s)"
  xr_h=$(xr_bounded "$XR_T_HASH" sha256sum "$xr_a" 2>>"$XR_LOG")
  xr_rc=$?
  [ "$xr_rc" -eq 0 ] || { xr_fail ARCHIVE_HASH "sha256sum rc=$xr_rc (124 = timed out)"; return 1; }
  [ "${xr_h%% *}" = "$XR_A_SHA" ] || { xr_fail ARCHIVE_HASH "sha256 ${xr_h%% *} is not the pinned official V2.43 archive"; return 1; }   #G:archive_sha

  XR_STAGE=$(mktemp -d "$XR_M/.XM5RX3-stage.XXXXXX" 2>>"$XR_LOG")
  if [ -z "$XR_STAGE" ] || [ ! -d "$XR_STAGE" ] || [ -L "$XR_STAGE" ]; then XR_STAGE=; xr_fail STAGE_FAILED "cannot create private staging dir"; return 1; fi
  xr_log "extracting into ${XR_STAGE##*/} (limit ${XR_T_TAR}s)"
  xr_bounded "$XR_T_TAR" tar -x -o -f "$xr_a" -C "$XR_STAGE" >>"$XR_LOG" 2>&1
  xr_rc=$?
  [ "$xr_rc" -ne 124 ] || { xr_fail EXTRACT_TIMEOUT "tar killed after ${XR_T_TAR}s"; xr_stage_cleanup; return 1; }
  [ "$xr_rc" -eq 0 ] || { xr_fail EXTRACT_FAILED "tar rc=$xr_rc"; xr_stage_cleanup; return 1; }   #G:extract_rc
  { [ "$(ls -A "$XR_STAGE" 2>/dev/null | wc -l | tr -d ' ')" = 2 ] && xr_rudir_ok "$XR_STAGE"; } || { xr_fail STAGE_CONTENT "staging does not hold exactly the expected entries"; xr_stage_cleanup; return 1; }   #G:stage_list
  xr_files_ok "$XR_STAGE" || { xr_fail STAGE_HASH "extracted files differ from pinned members"; xr_stage_cleanup; return 1; }   #G:stage_hash
  xr_no_conflict || { xr_fail PUBLISH_CONFLICT "recovery files appeared at card root during staging"; xr_stage_cleanup; return 1; }   #G:conflict_post

  xr_log "reading ota backup (read-only)"
  xr_bounded "$XR_T_MTD" "$XR_NANDDUMP" --bb=padbad -f "$XR_RUN/ota-before.bin" "$XR_DEV/mtd6" >>"$XR_LOG" 2>&1
  xr_rc=$?
  { [ "$xr_rc" -eq 0 ] && [ -f "$XR_RUN/ota-before.bin" ] && [ "$(xr_size "$XR_RUN/ota-before.bin")" = "$XR_OTA_SIZE" ]; } || { xr_fail OTA_BACKUP_FAILED "nanddump mtd6 rc=$xr_rc or wrong size"; xr_stage_cleanup; return 1; }   #G:ota_backup
  printf '%-256s' 'ota:kernel2' >"$XR_RUN/ota-flag.bin" 2>>"$XR_LOG"
  { [ "$(xr_size "$XR_RUN/ota-flag.bin")" = 256 ] && [ "$(head -c 11 "$XR_RUN/ota-flag.bin")" = ota:kernel2 ]; } || { xr_fail FLAG_PREP_FAILED "cannot prepare 256-byte flag file"; xr_stage_cleanup; return 1; }

  xr_log "publishing pinned files at card root"
  mv "$XR_STAGE/recovery-update" "$XR_M/recovery-update" >>"$XR_LOG" 2>&1 && xr_pub=d &&
    mv "$XR_STAGE/firmware_v20.info" "$XR_M/firmware_v20.info" >>"$XR_LOG" 2>&1 && xr_pub=df
  [ "$xr_pub" = df ] || { xr_fail PUBLISH_FAILED "rename into card root failed"; xr_retract; xr_stage_cleanup; return 1; }
  { xr_rudir_ok "$XR_M" && xr_files_ok "$XR_M" && sync; } || { xr_fail PUBLISH_VERIFY_FAILED "published files do not match pinned members"; xr_retract; xr_stage_cleanup; return 1; }   #G:publish_verify
  [ "$XR_RESCUE_WRITE" = 1 ] || { xr_fail RESCUE_WRITE_DISABLED "all pre-write checks passed; ota write disabled in this build"; xr_retract; xr_stage_cleanup; return 1; }   #G:write_gate
  rmdir "$XR_STAGE" 2>>"$XR_LOG" || xr_log "empty staging ${XR_STAGE##*/} could not be removed (harmless)"
  XR_STAGE=
  xr_ota_layout || { xr_fail MTD_LAYOUT "ota partition check failed before write; publication left in place"; return 1; }

  xr_log "DESTRUCTIVE BOUNDARY: flash_erase mtd6 block 0, then nandwrite ota:kernel2. Not transactional; no automatic restore. Backup: ${XR_RUN##*/}/ota-before.bin"
  xr_result DESTRUCTIVE_STARTED
  sync
  xr_bounded "$XR_T_MTD" "$XR_FLASH_ERASE" "$XR_DEV/mtd6" 0 1 >>"$XR_LOG" 2>&1
  xr_rc=$?
  [ "$xr_rc" -eq 0 ] || { xr_fail ERASE_FAILED "flash_erase rc=$xr_rc (124 = timed out); ota block state UNKNOWN (may be erased or partial); no write attempted; normal boot and recovery are NOT assured"; return 1; }   #G:erase_rc
  xr_bounded "$XR_T_MTD" "$XR_NANDWRITE" -s 0 -p "$XR_DEV/mtd6" "$XR_RUN/ota-flag.bin" >>"$XR_LOG" 2>&1
  xr_rc=$?
  [ "$xr_rc" -eq 0 ] || { xr_fail WRITE_FAILED "nandwrite rc=$xr_rc after a successful erase; ota block is erased or partially written; no reboot requested; normal boot and recovery are NOT assured"; return 1; }   #G:write_rc
  xr_bounded "$XR_T_MTD" "$XR_MTD_DEBUG" read "$XR_DEV/mtd6" 0 256 "$XR_RUN/ota-readback.bin" >>"$XR_LOG" 2>&1
  xr_rc=$?
  [ "$xr_rc" -eq 0 ] || { xr_fail READBACK_FAILED "mtd_debug read rc=$xr_rc; ota content unconfirmed; no reboot requested; normal boot and recovery are NOT assured"; return 1; }
  cmp -s "$XR_RUN/ota-readback.bin" "$XR_RUN/ota-flag.bin" || { xr_fail READBACK_MISMATCH "ota readback differs from the flag; no reboot requested; normal boot and recovery are NOT assured"; return 1; }   #G:readback_cmp
  xr_result FLAG_VERIFIED_REBOOTING
  xr_log "ota:kernel2 written and read back; requesting reboot (kernel2 behaviour itself is unverified)"
  sync
  "$XR_REBOOT" >>"$XR_LOG" 2>&1
  xr_rc=$?
  [ "$xr_rc" -eq 0 ] || { xr_fail REBOOT_COMMAND_FAILED "reboot rc=$xr_rc; ota:kernel2 stays set, so the next boot should enter kernel2 (unverified)"; return 1; }   #G:reboot_rc
  xr_i=0
  while [ "$xr_i" -lt "$XR_T_REBOOT" ]; do sleep 1; xr_i=$((xr_i + 1)); done
  xr_fail REBOOT_NOT_OBSERVED "reboot returned 0 but this shell still runs after ${XR_T_REBOOT}s (init may defer it until rcS ends); ota:kernel2 stays set"
  return 1
}

xr_main() {
  xr_bad=0
  for xr_t in XM5_DUMP XM5_RESCUE; do
    if [ -e "$XR_M/$xr_t" ] || [ -L "$XR_M/$xr_t" ]; then
      { [ -f "$XR_M/$xr_t" ] && [ ! -L "$XR_M/$xr_t" ]; } || xr_bad=1   #G:trigger_type
    fi
  done
  [ "$xr_bad" = 0 ] || { xr_fail TRIGGER_BAD_TYPE "triggers must be regular files (not symlink/dir/other); left untouched"; return 1; }
  if [ "$1" = 1 ]; then xr_consume XM5_DUMP || { xr_fail CONSUME_FAILED "XM5_DUMP not durably consumed; nothing done"; return 1; }; fi
  if [ "$2" = 1 ]; then xr_consume XM5_RESCUE || { xr_fail CONSUME_FAILED "XM5_RESCUE not durably consumed; nothing done"; return 1; }; fi
  [ "$1$2" != 11 ] || { xr_fail DUAL_TRIGGER "both XM5_DUMP and XM5_RESCUE present; both consumed, nothing done"; return 1; }   #G:dual
  if [ "$1" = 1 ]; then xr_dump; else xr_rescue; fi
}

xm5_rx3_hook() {
  # fixed configuration; RX3 tests rewrite only this block, in a copy
  XR_M=/mnt/mmc
  XR_PROC=/proc
  XR_DEV=/dev
  XR_TMP=/tmp
  XR_PATH=/bin:/sbin:/usr/bin:/usr/sbin
  XR_NANDDUMP=/usr/sbin/nanddump
  XR_FLASH_ERASE=/usr/sbin/flash_erase
  XR_NANDWRITE=/usr/sbin/nandwrite
  XR_MTD_DEBUG=/usr/sbin/mtd_debug
  XR_REBOOT=/sbin/reboot
  XR_MOUNT_WAIT=10
  XR_T_HASH=600
  XR_T_TAR=900
  XR_T_MTD=60
  XR_T_DUMP_BASE=120
  XR_T_REBOOT=20
  XR_MARGIN_KB=8192
  XR_RESCUE_WRITE=1
  # end of fixed configuration
  # pinned official ONIX XM5 V2.43 update.bin and its members
  XR_A_NAME=XM5_STOCK_V2.43.bin
  XR_A_SIZE=56371200
  XR_A_SHA=8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4
  XR_A_UNPACKED=56365258
  XR_I_SIZE=138
  XR_I_SHA=7f347da148c743c185a41f1d350dc08645ed71bcfa3678c4f57e0babffa303f6
  XR_K_SIZE=5410880
  XR_K_SHA=088be0f3844d5cec4d0ce699c3a784912839e36e039cc0cffff07468f9415eaf
  XR_R_SIZE=50954240
  XR_R_SHA=9f9218496547bd8cee111e2f97aa09b724027d00724f2434ced23e2e0ab2d46c

  PATH=$XR_PATH; export PATH
  XR_LOG=$XR_TMP/xm5rx3.log
  XR_RUN=; XR_RESULT=; XR_OTA_SIZE=0
  ls -d "$XR_DEV"/mmcblk[0-9]* >/dev/null 2>&1 || return 0
  xr_i=0
  while :; do
    xr_mnt=$(awk -v m="$XR_M" '$2 == m { s = $1; o = $4 } END { if (s != "") print s, o }' "$XR_PROC/mounts" 2>/dev/null)
    [ -n "$xr_mnt" ] || [ "$xr_i" -ge "$XR_MOUNT_WAIT" ] && break
    sleep 1
    xr_i=$((xr_i + 1))
  done
  [ -n "$xr_mnt" ] || { xr_log "$(date) card device present but $XR_M not mounted after ${xr_i}s; hook idle"; return 0; }
  xr_td=0; xr_tr=0
  { [ -e "$XR_M/XM5_DUMP" ] || [ -L "$XR_M/XM5_DUMP" ]; } && xr_td=1
  { [ -e "$XR_M/XM5_RESCUE" ] || [ -L "$XR_M/XM5_RESCUE" ]; } && xr_tr=1
  [ "$xr_td$xr_tr" != 00 ] || return 0
  set -- $xr_mnt
  case $1 in /dev/mmcblk[0-9]|/dev/mmcblk[0-9]p[0-9]|/dev/mmcblk[0-9]p[0-9][0-9]) xr_ok=1 ;; *) xr_ok=0 ;; esac
  [ "$xr_ok" = 1 ] || { xr_log "$(date) $XR_M is mounted from $1, not a TF card; trigger ignored"; return 0; }   #G:mount_src
  case ",$2," in *,rw,*) xr_ok=1 ;; *) xr_ok=0 ;; esac
  [ "$xr_ok" = 1 ] || { xr_log "$(date) $XR_M is not mounted read-write ($2); trigger ignored"; return 0; }   #G:mount_rw
  if [ ! -d "$XR_M" ] || [ -L "$XR_M" ]; then xr_log "$(date) $XR_M is not a directory; trigger ignored"; return 0; fi
  XR_RUN=$(mktemp -d "$XR_M/XM5RX3-run.XXXXXX" 2>/dev/null)
  if [ -z "$XR_RUN" ] || [ ! -d "$XR_RUN" ] || [ -L "$XR_RUN" ]; then XR_RUN=; xr_log "$(date) cannot create a run dir on $XR_M (not writable?); trigger ignored"; return 0; fi
  if ! { echo probe >"$XR_RUN/probe" && [ "$(cat "$XR_RUN/probe")" = probe ] && rm -f "$XR_RUN/probe"; } 2>/dev/null; then
    XR_RUN=; xr_log "$(date) write probe on $XR_M failed; trigger ignored"; return 0
  fi
  XR_LOG=$XR_RUN/log.txt
  xr_log "== xm5 rx3 hook $(date) run=${XR_RUN##*/} card=$1 opts=$2 dump=$xr_td rescue=$xr_tr"
  xr_main "$xr_td" "$xr_tr"
  [ -n "$XR_RESULT" ] || xr_fail INTERNAL_ERROR "no result recorded"
  echo "$(date) ${XR_RUN##*/} $XR_RESULT" >>"$XR_M/XM5RX3.log" 2>/dev/null
  sync
  return 0
}
# <<< xm5-rx3-hook end
