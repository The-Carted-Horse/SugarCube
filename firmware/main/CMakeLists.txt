idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       REQUIRES gc_board gc_config gc_contract gc_httpd gc_net
                                gc_ota gc_sources gc_store gc_ui
                                nvs_flash esp_netif esp_event esp_timer)
