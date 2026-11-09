#ifndef XM5_OTHER_CONTAINER_H
#define XM5_OTHER_CONTAINER_H

#include "../inline_flac.h"

/* Return 1 for a validated payload span, 0 if absent, or -1 on failure.
 * The reported header is four bytes before the XM5I marker, for xm5_slot(). */
int xm5_other_find(int fd, struct xm5_block *block);

#endif
