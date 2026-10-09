/* pw_bridge.c
 *
 * C wrapper exposing the PipeWire functions needed by applications
 * that cannot call them directly through ctypes (static inline
 * functions in the PipeWire headers, or functions requiring
 * per-instance user_data routing).
 *
 * This wrapper is designed to be reusable: it has no dependency
 * on any specific application and only requires libpipewire.
 *
 * GIL handling: all callbacks invoked from the PipeWire thread loop
 * acquire the Python GIL via PyGILState_Ensure / PyGILState_Release.
 *
 * Only functions that are static inline in the PipeWire headers are
 * wrapped here. Anything exported by libpipewire-0.3.so.0 (such as
 * pw_proxy_destroy, pw_core_disconnect, pw_context_new, etc.) is
 * called directly via ctypes and does not appear in this file.
 *
 * Build:
 *   make
 */

#include <pipewire/pipewire.h>
#include <pipewire/extensions/metadata.h>
#include <spa/utils/hook.h>
#include <spa/utils/dict.h>
#include <spa/pod/iter.h>
#include <spa/pod/builder.h>
#include <spa/param/props.h>
#include <spa/param/param.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <Python.h>


/* ====================================================================
 * Library version
 * ==================================================================== */

const char *pw_bridge_version(void)
{
    return pw_get_library_version();
}

uint32_t pw_bridge_version_registry(void) { return PW_VERSION_REGISTRY; }
uint32_t pw_bridge_version_core(void)     { return PW_VERSION_CORE; }
uint32_t pw_bridge_version_link(void)     { return PW_VERSION_LINK; }
uint32_t pw_bridge_version_node(void)     { return PW_VERSION_NODE; }
uint32_t pw_bridge_version_port(void)     { return PW_VERSION_PORT; }
uint32_t pw_bridge_version_client(void)   { return PW_VERSION_CLIENT; }
uint32_t pw_bridge_version_device(void)   { return PW_VERSION_DEVICE; }

#ifdef PW_VERSION_METADATA
uint32_t pw_bridge_version_metadata(void) { return PW_VERSION_METADATA; }
#endif


/* ====================================================================
 * Registry
 * ==================================================================== */

struct pw_registry *pw_bridge_get_registry(struct pw_core *core)
{
    return pw_core_get_registry(core, PW_VERSION_REGISTRY, 0);
}

int pw_bridge_registry_destroy(struct pw_registry *registry, uint32_t id)
{
    if (registry == NULL)
        return -1;
    return pw_registry_destroy(registry, id);
}

int pw_bridge_destroy_link(struct pw_registry *registry, uint32_t link_id)
{
    return pw_bridge_registry_destroy(registry, link_id);
}


/* ====================================================================
 * Registry listener
 * ==================================================================== */

typedef void (*pw_bridge_global_cb_t)(
    void *user_data, uint32_t id, uint32_t permissions,
    const char *type, uint32_t version, const struct spa_dict *props);
typedef void (*pw_bridge_global_remove_cb_t)(void *user_data, uint32_t id);

struct pw_bridge_registry_listener {
    struct spa_hook               hook;
    pw_bridge_global_cb_t         global_cb;
    pw_bridge_global_remove_cb_t  global_remove_cb;
    void                         *user_data;
};

static void _registry_global_relay(void *data, uint32_t id,
                                   uint32_t permissions,
                                   const char *type, uint32_t version,
                                   const struct spa_dict *props)
{
    struct pw_bridge_registry_listener *l = data;
    if (l->global_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->global_cb(l->user_data, id, permissions, type, version, props);
    PyGILState_Release(gil);
}

static void _registry_global_remove_relay(void *data, uint32_t id)
{
    struct pw_bridge_registry_listener *l = data;
    if (l->global_remove_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->global_remove_cb(l->user_data, id);
    PyGILState_Release(gil);
}

static const struct pw_registry_events g_registry_events = {
    .version       = PW_VERSION_REGISTRY_EVENTS,
    .global        = _registry_global_relay,
    .global_remove = _registry_global_remove_relay,
};

struct pw_bridge_registry_listener *
pw_bridge_registry_listener_new(struct pw_registry *registry,
                                pw_bridge_global_cb_t global_cb,
                                pw_bridge_global_remove_cb_t global_remove_cb,
                                void *user_data)
{
    if (registry == NULL)
        return NULL;
    struct pw_bridge_registry_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->global_cb        = global_cb;
    l->global_remove_cb = global_remove_cb;
    l->user_data        = user_data;
    pw_registry_add_listener(registry, &l->hook, &g_registry_events, l);
    return l;
}

void pw_bridge_registry_listener_free(struct pw_bridge_registry_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}


/* ====================================================================
 * Core listener
 * ==================================================================== */

typedef void (*pw_bridge_core_info_cb_t)(void *user_data, const char *name,
                                         const char *version,
                                         uint32_t change_mask);
typedef void (*pw_bridge_core_done_cb_t)(void *user_data, uint32_t id, int seq);
typedef void (*pw_bridge_core_error_cb_t)(void *user_data, uint32_t id, int seq,
                                          int res, const char *message);

struct pw_bridge_core_listener {
    struct spa_hook            hook;
    pw_bridge_core_info_cb_t   info_cb;
    pw_bridge_core_done_cb_t   done_cb;
    pw_bridge_core_error_cb_t  error_cb;
    void                      *user_data;
};

static void _core_info_relay(void *data, const struct pw_core_info *info)
{
    struct pw_bridge_core_listener *l = data;
    if (l->info_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->info_cb(l->user_data,
               info->name ? info->name : "",
               info->version ? info->version : "",
               info->change_mask);
    PyGILState_Release(gil);
}

static void _core_done_relay(void *data, uint32_t id, int seq)
{
    struct pw_bridge_core_listener *l = data;
    if (l->done_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->done_cb(l->user_data, id, seq);
    PyGILState_Release(gil);
}

static void _core_error_relay(void *data, uint32_t id, int seq,
                              int res, const char *message)
{
    struct pw_bridge_core_listener *l = data;
    if (l->error_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->error_cb(l->user_data, id, seq, res, message);
    PyGILState_Release(gil);
}

static const struct pw_core_events g_core_events = {
    .version = PW_VERSION_CORE_EVENTS,
    .info    = _core_info_relay,
    .done    = _core_done_relay,
    .error   = _core_error_relay,
};

struct pw_bridge_core_listener *
pw_bridge_core_listener_new(struct pw_core *core,
                            pw_bridge_core_info_cb_t info_cb,
                            pw_bridge_core_done_cb_t done_cb,
                            pw_bridge_core_error_cb_t error_cb,
                            void *user_data)
{
    if (core == NULL)
        return NULL;
    struct pw_bridge_core_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->info_cb   = info_cb;
    l->done_cb   = done_cb;
    l->error_cb  = error_cb;
    l->user_data = user_data;
    pw_core_add_listener(core, &l->hook, &g_core_events, l);
    return l;
}

void pw_bridge_core_listener_free(struct pw_bridge_core_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}


/* ====================================================================
 * Node proxy: bind + info listener + param listener + enum/set param
 *
 * The info callback now exposes the node's params list. Each entry
 * is a (id, flags) pair. This lets the Python side know which params
 * the node supports, in particular SPA_PARAM_Props, before issuing
 * an enum_params that would otherwise fail with -ENOENT.
 * ==================================================================== */

typedef void (*pw_bridge_node_info_cb_t)(
    void *user_data, uint32_t node_id,
    uint32_t max_input_ports, uint32_t max_output_ports,
    uint32_t change_mask, int state, const char *error,
    const struct spa_dict *props,
    const struct spa_param_info *params, uint32_t n_params);

typedef void (*pw_bridge_node_param_cb_t)(
    void *user_data, uint32_t node_id, int seq,
    uint32_t id, uint32_t index, uint32_t next,
    const struct spa_pod *param);

struct pw_bridge_node_listener {
    struct spa_hook             hook;
    pw_bridge_node_info_cb_t    info_cb;
    pw_bridge_node_param_cb_t   param_cb;
    void                       *user_data;
};

static void _node_info_relay(void *data, const struct pw_node_info *info)
{
    struct pw_bridge_node_listener *l = data;
    if (l->info_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->info_cb(l->user_data,
               info->id,
               info->max_input_ports,
               info->max_output_ports,
               info->change_mask,
               info->state,
               info->error ? info->error : "",
               info->props,
               info->params,
               info->n_params);
    PyGILState_Release(gil);
}

static void _node_param_relay(void *data, int seq, uint32_t id,
                              uint32_t index, uint32_t next,
                              const struct spa_pod *param)
{
    struct pw_bridge_node_listener *l = data;
    if (l->param_cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->param_cb(l->user_data, 0, seq, id, index, next, param);
    PyGILState_Release(gil);
}

static const struct pw_node_events g_node_events = {
    .version = PW_VERSION_NODE_EVENTS,
    .info    = _node_info_relay,
    .param   = _node_param_relay,
};

struct pw_proxy *pw_bridge_bind_node(struct pw_registry *registry,
                                     uint32_t node_id)
{
    if (registry == NULL)
        return NULL;
    return (struct pw_proxy *)pw_registry_bind(
        registry, node_id, PW_TYPE_INTERFACE_Node, PW_VERSION_NODE, 0);
}

struct pw_bridge_node_listener *
pw_bridge_node_listener_new(struct pw_proxy *proxy,
                            pw_bridge_node_info_cb_t info_cb,
                            pw_bridge_node_param_cb_t param_cb,
                            void *user_data)
{
    if (proxy == NULL)
        return NULL;
    struct pw_bridge_node_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->info_cb   = info_cb;
    l->param_cb  = param_cb;
    l->user_data = user_data;
    pw_node_add_listener((struct pw_node *)proxy, &l->hook,
                         &g_node_events, l);
    return l;
}

void pw_bridge_node_listener_free(struct pw_bridge_node_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}

int pw_bridge_node_enum_params(struct pw_proxy *proxy, int seq,
                               uint32_t id, uint32_t start,
                               uint32_t num, const struct spa_pod *filter)
{
    if (proxy == NULL)
        return -1;
    return pw_node_enum_params((struct pw_node *)proxy, seq, id,
                               start, num, filter);
}

int pw_bridge_node_set_param(struct pw_proxy *proxy, uint32_t id,
                             uint32_t flags, const struct spa_pod *param)
{
    if (proxy == NULL)
        return -1;
    return pw_node_set_param((struct pw_node *)proxy, id, flags, param);
}

/* Copy the ids of a params list into out_ids. Returns the number of
 * ids copied (<= max_ids). */
uint32_t pw_bridge_node_param_ids(const struct spa_param_info *params,
                                  uint32_t n_params,
                                  uint32_t *out_ids,
                                  uint32_t max_ids)
{
    uint32_t n = 0;
    if (params == NULL || out_ids == NULL)
        return 0;
    for (uint32_t i = 0; i < n_params && n < max_ids; i++)
        out_ids[n++] = params[i].id;
    return n;
}


/* ====================================================================
 * Port proxy: bind + listener
 * ==================================================================== */

typedef void (*pw_bridge_port_info_cb_t)(
    void *user_data, uint32_t port_id, uint32_t direction,
    uint32_t change_mask, const struct spa_dict *props);

struct pw_bridge_port_listener {
    struct spa_hook            hook;
    pw_bridge_port_info_cb_t   cb;
    void                      *user_data;
};

static void _port_info_relay(void *data, const struct pw_port_info *info)
{
    struct pw_bridge_port_listener *l = data;
    if (l->cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->cb(l->user_data, info->id, info->direction,
          info->change_mask, info->props);
    PyGILState_Release(gil);
}

static const struct pw_port_events g_port_events = {
    .version = PW_VERSION_PORT_EVENTS,
    .info    = _port_info_relay,
};

struct pw_proxy *pw_bridge_bind_port(struct pw_registry *registry,
                                     uint32_t port_id)
{
    if (registry == NULL)
        return NULL;
    return (struct pw_proxy *)pw_registry_bind(
        registry, port_id, PW_TYPE_INTERFACE_Port, PW_VERSION_PORT, 0);
}

struct pw_bridge_port_listener *
pw_bridge_port_listener_new(struct pw_proxy *proxy,
                            pw_bridge_port_info_cb_t cb,
                            void *user_data)
{
    if (proxy == NULL)
        return NULL;
    struct pw_bridge_port_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->cb        = cb;
    l->user_data = user_data;
    pw_port_add_listener((struct pw_port *)proxy, &l->hook,
                         &g_port_events, l);
    return l;
}

void pw_bridge_port_listener_free(struct pw_bridge_port_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}


/* ====================================================================
 * Link proxy: bind + listener + creation
 * ==================================================================== */

typedef void (*pw_bridge_link_info_cb_t)(
    void *user_data, uint32_t link_id,
    uint32_t output_node_id, uint32_t output_port_id,
    uint32_t input_node_id, uint32_t input_port_id,
    int state, const char *error, const struct spa_dict *props);

struct pw_bridge_link_listener {
    struct spa_hook            hook;
    pw_bridge_link_info_cb_t   cb;
    void                      *user_data;
};

static void _link_info_relay(void *data, const struct pw_link_info *info)
{
    struct pw_bridge_link_listener *l = data;
    if (l->cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->cb(l->user_data, info->id,
          info->output_node_id, info->output_port_id,
          info->input_node_id, info->input_port_id,
          info->state, info->error ? info->error : "", info->props);
    PyGILState_Release(gil);
}

static const struct pw_link_events g_link_events = {
    .version = PW_VERSION_LINK_EVENTS,
    .info    = _link_info_relay,
};

struct pw_proxy *pw_bridge_bind_link(struct pw_registry *registry,
                                     uint32_t link_id)
{
    if (registry == NULL)
        return NULL;
    return (struct pw_proxy *)pw_registry_bind(
        registry, link_id, PW_TYPE_INTERFACE_Link, PW_VERSION_LINK, 0);
}

struct pw_bridge_link_listener *
pw_bridge_link_listener_new(struct pw_proxy *proxy,
                            pw_bridge_link_info_cb_t cb,
                            void *user_data)
{
    if (proxy == NULL)
        return NULL;
    struct pw_bridge_link_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->cb        = cb;
    l->user_data = user_data;
    pw_link_add_listener((struct pw_link *)proxy, &l->hook,
                         &g_link_events, l);
    return l;
}

void pw_bridge_link_listener_free(struct pw_bridge_link_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}

struct pw_proxy *pw_bridge_create_link(struct pw_core *core,
                                       uint32_t out_port_id,
                                       uint32_t in_port_id)
{
    if (core == NULL)
        return NULL;
    char out_buf[16], in_buf[16];
    struct spa_dict_item items[2];
    struct spa_dict props;
    snprintf(out_buf, sizeof(out_buf), "%u", out_port_id);
    snprintf(in_buf, sizeof(in_buf), "%u", in_port_id);
    items[0].key = "link.output.port"; items[0].value = out_buf;
    items[1].key = "link.input.port";  items[1].value = in_buf;
    props.flags = 0; props.n_items = 2; props.items = items;
    return pw_core_create_object(core, "link-factory",
                                 PW_TYPE_INTERFACE_Link,
                                 PW_VERSION_LINK, &props, 0);
}


/* ====================================================================
 * Client proxy
 * ==================================================================== */

struct pw_proxy *pw_bridge_bind_client(struct pw_registry *registry,
                                       uint32_t client_id)
{
    if (registry == NULL)
        return NULL;
    return (struct pw_proxy *)pw_registry_bind(
        registry, client_id, PW_TYPE_INTERFACE_Client, PW_VERSION_CLIENT, 0);
}

int pw_bridge_client_update_properties(struct pw_proxy *proxy,
                                       const struct spa_dict *props)
{
    if (proxy == NULL)
        return -1;
    pw_client_update_properties((struct pw_client *)proxy, props);
    return 0;
}


/* ====================================================================
 * Device proxy
 * ==================================================================== */

typedef void (*pw_bridge_device_info_cb_t)(
    void *user_data, uint32_t device_id, uint32_t change_mask,
    const struct spa_dict *props);

struct pw_bridge_device_listener {
    struct spa_hook             hook;
    pw_bridge_device_info_cb_t  cb;
    void                       *user_data;
};

static void _device_info_relay(void *data, const struct pw_device_info *info)
{
    struct pw_bridge_device_listener *l = data;
    if (l->cb == NULL)
        return;
    PyGILState_STATE gil = PyGILState_Ensure();
    l->cb(l->user_data, info->id, info->change_mask, info->props);
    PyGILState_Release(gil);
}

static const struct pw_device_events g_device_events = {
    .version = PW_VERSION_DEVICE_EVENTS,
    .info    = _device_info_relay,
};

struct pw_proxy *pw_bridge_bind_device(struct pw_registry *registry,
                                       uint32_t device_id)
{
    if (registry == NULL)
        return NULL;
    return (struct pw_proxy *)pw_registry_bind(
        registry, device_id, PW_TYPE_INTERFACE_Device, PW_VERSION_DEVICE, 0);
}

struct pw_bridge_device_listener *
pw_bridge_device_listener_new(struct pw_proxy *proxy,
                              pw_bridge_device_info_cb_t cb,
                              void *user_data)
{
    if (proxy == NULL)
        return NULL;
    struct pw_bridge_device_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->cb        = cb;
    l->user_data = user_data;
    pw_device_add_listener((struct pw_device *)proxy, &l->hook,
                           &g_device_events, l);
    return l;
}

void pw_bridge_device_listener_free(struct pw_bridge_device_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}


/* ====================================================================
 * Metadata proxy
 * ==================================================================== */

#ifdef PW_TYPE_INTERFACE_Metadata

typedef int (*pw_bridge_metadata_property_cb_t)(
    void *user_data, uint32_t metadata_id, uint32_t subject,
    const char *key, const char *type, const char *value);

struct pw_bridge_metadata_listener {
    struct spa_hook                    hook;
    pw_bridge_metadata_property_cb_t   cb;
    void                              *user_data;
    uint32_t                           metadata_id;
};

static int _metadata_property_relay(void *data, uint32_t subject,
                                    const char *key, const char *type,
                                    const char *value)
{
    struct pw_bridge_metadata_listener *l = data;
    if (l->cb == NULL)
        return 0;
    PyGILState_STATE gil = PyGILState_Ensure();
    int ret = l->cb(l->user_data, l->metadata_id, subject,
                    key ? key : "", type ? type : "", value);
    PyGILState_Release(gil);
    return ret;
}

static const struct pw_metadata_events g_metadata_events = {
    .version  = PW_VERSION_METADATA_EVENTS,
    .property = _metadata_property_relay,
};

struct pw_proxy *pw_bridge_bind_metadata(struct pw_registry *registry,
                                         uint32_t metadata_id)
{
    if (registry == NULL)
        return NULL;
    return (struct pw_proxy *)pw_registry_bind(
        registry, metadata_id,
        PW_TYPE_INTERFACE_Metadata, PW_VERSION_METADATA, 0);
}

struct pw_bridge_metadata_listener *
pw_bridge_metadata_listener_new(struct pw_proxy *proxy,
                                pw_bridge_metadata_property_cb_t cb,
                                void *user_data,
                                uint32_t metadata_id)
{
    if (proxy == NULL)
        return NULL;
    struct pw_bridge_metadata_listener *l = calloc(1, sizeof(*l));
    if (l == NULL)
        return NULL;
    l->cb          = cb;
    l->user_data   = user_data;
    l->metadata_id = metadata_id;
    pw_metadata_add_listener((struct pw_metadata *)proxy, &l->hook,
                             &g_metadata_events, l);
    return l;
}

void pw_bridge_metadata_listener_free(struct pw_bridge_metadata_listener *l)
{
    if (l == NULL)
        return;
    spa_hook_remove(&l->hook);
    free(l);
}

int pw_bridge_metadata_set_property(struct pw_proxy *proxy,
                                    uint32_t subject,
                                    const char *key, const char *type,
                                    const char *value)
{
    /* pw_metadata_set_property is a macro in
     * <pipewire/extensions/metadata.h>, not an exported symbol of
     * libpipewire-0.3.so.0 (verify with `nm -D | grep metadata`).
     * The wrapper is therefore required by the rule stated in the
     * API reference, section 1.1. Do not attempt to call it via
     * ctypes directly. */
    if (proxy == NULL)
        return -1;
    return pw_metadata_set_property((struct pw_metadata *)proxy,
                                    subject, key, type, value);
}

#endif /* PW_TYPE_INTERFACE_Metadata */


/* ====================================================================
 * SPA pod parsing helpers for SPA_PARAM_Props
 * ==================================================================== */

#define PW_BRIDGE_PROPS_HAS_VOLUME   (1u << 0)
#define PW_BRIDGE_PROPS_HAS_MUTE     (1u << 1)
#define PW_BRIDGE_PROPS_HAS_CHANNELS (1u << 2)

uint32_t pw_bridge_pod_parse_props(const struct spa_pod *pod,
                                   float *out_volume,
                                   int   *out_mute,
                                   float *out_channels,
                                   uint32_t max_channels,
                                   uint32_t *out_n_channels)
{
    uint32_t found = 0;
    if (pod == NULL)
        return 0;

    if (out_n_channels)
        *out_n_channels = 0;

    if (!spa_pod_is_object(pod))
        return 0;
    const struct spa_pod_object *obj =
        (const struct spa_pod_object *)pod;
    if (obj->body.id != SPA_PARAM_Props)
        return 0;

    const struct spa_pod_prop *prop;
    SPA_POD_OBJECT_FOREACH(obj, prop) {
        if (prop->key == SPA_PROP_volume) {
            float v;
            if (spa_pod_get_float(&prop->value, &v) == 0) {
                if (out_volume) *out_volume = v;
                found |= PW_BRIDGE_PROPS_HAS_VOLUME;
            }
        } else if (prop->key == SPA_PROP_mute) {
            bool m;
            if (spa_pod_get_bool(&prop->value, &m) == 0) {
                if (out_mute) *out_mute = m ? 1 : 0;
                found |= PW_BRIDGE_PROPS_HAS_MUTE;
            }
        } else if (prop->key == SPA_PROP_channelVolumes) {
            if (out_channels && max_channels > 0) {
                uint32_t n = 0;
                const void *data = spa_pod_get_array(&prop->value, &n);
                if (data != NULL && n > 0) {
                    const float *vals = (const float *)data;
                    if (n > max_channels)
                        n = max_channels;
                    memcpy(out_channels, vals, n * sizeof(float));
                    if (out_n_channels)
                        *out_n_channels = n;
                    found |= PW_BRIDGE_PROPS_HAS_CHANNELS;
                }
            }
        }
    }

    return found;
}

const struct spa_pod *pw_bridge_pod_build_props(void *buffer,
                                                size_t buffer_size,
                                                uint32_t bits,
                                                float volume,
                                                int mute,
                                                const float *channels,
                                                uint32_t n_channels)
{
    if (buffer == NULL || buffer_size == 0)
        return NULL;

    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, buffer_size);
    struct spa_pod_frame f;

    spa_pod_builder_push_object(&b, &f, SPA_TYPE_OBJECT_Props,
                                SPA_PARAM_Props);

    if (bits & PW_BRIDGE_PROPS_HAS_VOLUME) {
        spa_pod_builder_prop(&b, SPA_PROP_volume, 0);
        spa_pod_builder_float(&b, volume);
    }
    if (bits & PW_BRIDGE_PROPS_HAS_MUTE) {
        spa_pod_builder_prop(&b, SPA_PROP_mute, 0);
        spa_pod_builder_bool(&b, mute ? true : false);
    }
    if ((bits & PW_BRIDGE_PROPS_HAS_CHANNELS)
            && channels != NULL && n_channels > 0) {
        spa_pod_builder_prop(&b, SPA_PROP_channelVolumes, 0);
        spa_pod_builder_array(&b, sizeof(float), SPA_TYPE_Float,
                              n_channels, channels);
    }

    return spa_pod_builder_pop(&b, &f);
}


/* ====================================================================
 * Core sync
 * ==================================================================== */

int pw_bridge_core_sync(struct pw_core *core, uint32_t id, int seq)
{
    if (core == NULL)
        return -1;
    return pw_core_sync(core, id, seq);
}


/* ====================================================================
 * Thread loop helpers
 * ==================================================================== */

void pw_bridge_thread_loop_lock(void *thread_loop)
{
    if (thread_loop != NULL)
        pw_thread_loop_lock((struct pw_thread_loop *)thread_loop);
}

void pw_bridge_thread_loop_unlock(void *thread_loop)
{
    if (thread_loop != NULL)
        pw_thread_loop_unlock((struct pw_thread_loop *)thread_loop);
}