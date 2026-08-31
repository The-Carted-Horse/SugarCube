set(gc_net_requires
    gc_contract gc_config
    esp_wifi esp_netif esp_event nvs_flash lwip esp_timer)

# mDNS is not part of ESP-IDF; it comes from espressif/mdns, declared in
# main/idf_component.yml and fetched into managed_components/. The rest of
# gc_net works without it — the device is then reached at the IP the panel
# shows instead of at http://glucocube.local/ — so it is required only when
# the component manager has actually put it there, and gc_net.c compiles the
# mDNS calls in or out to match.
idf_build_get_property(gc_net_project_dir PROJECT_DIR)
if(EXISTS "${gc_net_project_dir}/managed_components/espressif__mdns/include/mdns.h")
    list(APPEND gc_net_requires espressif__mdns)
endif()

idf_component_register(SRCS "gc_net.c"
                       INCLUDE_DIRS "include"
                       REQUIRES ${gc_net_requires})
