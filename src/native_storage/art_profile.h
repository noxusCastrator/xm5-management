#ifndef XM5_ART_PROFILE_H
#define XM5_ART_PROFILE_H
#define PA_SIZES 9
#define PA_SIZE_MASK 511u
static const unsigned
    __attribute__((unused)) pa_sizes[PA_SIZES] = {46, 48, 80, 90, 100, 150, 160, 180, 330};
static inline int pa_size_slot(unsigned side) {
    unsigned i;
    for (i = 0; i < PA_SIZES; i++)
        if (side == pa_sizes[i])
            return (int)i;
    return -1;
}
#endif
