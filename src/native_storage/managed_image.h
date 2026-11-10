#ifndef XM5_MANAGED_IMAGE_H
#define XM5_MANAGED_IMAGE_H
#include "prepared_io.h"
int pa_managed_name(const char *);
void pa_note_manager(void *);
int pa_image_set(void *, const char *);
int pa_play_image_set(void *, const char *);
int pa_image_destroy(void *);
int pa_manager_destroy(void *);
int pa_image_paint(void *, void *);
int pa_need_transform(void *);
int pa_canvas_image(void *, void *, unsigned, const int *);
int pa_collect(void);
void pa_render_stamp(unsigned *, unsigned *);
int pa_render_valid(unsigned, unsigned);
#endif
