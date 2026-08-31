idf_component_register(SRCS "gc_sources.c" "gc_http.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "."
                       REQUIRES gc_contract gc_config gc_store
                                gc_synclog gc_net esp_http_client esp-tls json mbedtls
                                esp_timer)
