# The typefaces are embedded from glucocube/fonts/ rather than copied here:
# one set of font files in the repository, rendered by both products, under
# the SIL Open Font License that travels with them.
set(GC_FONT_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../glucocube/fonts")

idf_component_register(SRCS "gc_ui.c" "qrcodegen/qrcodegen.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "qrcodegen"
                       REQUIRES gc_contract gc_config gc_store gc_predict
                                gc_board lvgl esp_timer)

target_add_binary_data(${COMPONENT_LIB} "${GC_FONT_DIR}/SpaceGrotesk-Bold.ttf" BINARY)
target_add_binary_data(${COMPONENT_LIB} "${GC_FONT_DIR}/SpaceGrotesk-Medium.ttf" BINARY)
target_add_binary_data(${COMPONENT_LIB} "${GC_FONT_DIR}/JetBrainsMono-Regular.ttf" BINARY)
target_add_binary_data(${COMPONENT_LIB} "${GC_FONT_DIR}/JetBrainsMono-Medium.ttf" BINARY)
