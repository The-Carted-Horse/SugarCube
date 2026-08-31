# The board profile's header lives outside this component, in
# firmware/boards/, so that adding a board never means touching a driver.
idf_component_register(SRCS "gc_board.c"
                       INCLUDE_DIRS "include" "${CMAKE_CURRENT_LIST_DIR}/../.."
                       REQUIRES gc_contract esp_lcd driver esp_driver_gpio
                                esp_driver_i2c esp_driver_ledc)
