idf_component_register(SRCS "gc_ota.c" "gc_version.c"
                       INCLUDE_DIRS "include"
                       REQUIRES gc_contract esp_http_client esp-tls
                                esp_https_ota app_update json esp_timer
                                esp_partition)
