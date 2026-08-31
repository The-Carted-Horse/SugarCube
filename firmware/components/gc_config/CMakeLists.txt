idf_component_register(SRCS "gc_config.c"
                       INCLUDE_DIRS "include"
                       REQUIRES gc_contract gc_store nvs_flash json esp_hw_support)
