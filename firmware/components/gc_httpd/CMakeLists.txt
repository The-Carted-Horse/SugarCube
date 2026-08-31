idf_component_register(SRCS "gc_httpd.c"
                       INCLUDE_DIRS "include"
                       REQUIRES gc_contract gc_config gc_store gc_predict
                                gc_ui gc_net gc_sources gc_ota
                                gc_synclog esp_http_server json esp_timer mbedtls)
