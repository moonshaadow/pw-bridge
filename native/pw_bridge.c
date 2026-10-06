/* pw_bridge.c
 *
 * Minimal C wrapper exposing the PipeWire functions needed by
 * applications that cannot call them directly through ctypes
 * (static inline functions in the PipeWire headers).
 *
 * This wrapper is designed to be reusable: it has no dependency
 * on any specific application and only requires libpipewire.
 *
 * Build:
 *   make
 */

#include <pipewire/pipewire.h>
#include <spa/utils/hook.h>
#include <spa/utils/dict.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>


/* ====================================================================
 * Registry: creation
 * ==================================================================== */

struct pw_registry *pw_bridge_get_registry(struct pw_core *core)
{
    return pw_core_get_registry(core, PW_VERSION_REGISTRY, 0);
}


/* ====================================================================
 * Registry: listener
 * ==================================================================== */

typedef void (*pw_bridge_global_cb_t)(
    void *user_data,
    uint32_t id,
    uint32_t permissions,
    const char *type,
    uint32_t version,
    const struct spa_dict *props);

typedef void (*pw_bridge_global_remove_cb_t)(
    void *user_data,
    uint32_t id);

static pw_bridge_global_cb_t        g_global_cb        = NULL;
static pw_bridge_global_remove_cb_t g_global_remove_cb = NULL;
static void                        *g_user_data        = NULL;

static void _global_cb_relay(void *data, uint32_t id, uint32_t permissions,
                             const char *type, uint32_t version,
                             const struct spa_dict *props)
{
    if (g_global_cb != NULL)
        g_global_cb(g_user_data, id, permissions, type, version, props);
    (void)data;
}

static void _global_remove_cb_relay(void *data, uint32_t id)
{
    if (g_global_remove_cb != NULL)
        g_global_remove_cb(g_user_data, id);
    (void)data;
}

static struct pw_registry_events g_registry_events = {
    .version = PW_VERSION_REGISTRY_EVENTS,
    .global = _global_cb_relay,
    .global_remove = _global_remove_cb_relay,
};

void pw_bridge_set_registry_callbacks(
    pw_bridge_global_cb_t global_cb,
    pw_bridge_global_remove_cb_t global_remove_cb,
    void *user_data)
{
    g_global_cb = global_cb;
    g_global_remove_cb = global_remove_cb;
    g_user_data = user_data;
}

int pw_bridge_registry_add_listener(
    struct pw_registry *registry,
    struct spa_hook *hook)
{
    return pw_registry_add_listener(
        registry, hook, &g_registry_events, NULL);
}


/* ====================================================================
 * Core: listeners (diagnostic + disconnection detection)
 * ==================================================================== */

typedef void (*pw_bridge_core_error_cb_t)(
    void *user_data,
    uint32_t id,
    int seq,
    int res,
    const char *message);

static pw_bridge_core_error_cb_t g_core_error_cb = NULL;
static void                     *g_core_error_user_data = NULL;

static void _core_info_cb(void *data, const struct pw_core_info *info)
{
    fprintf(stderr, "[C] core info: name=%s version=%s\n",
            info->name ? info->name : "(null)",
            info->version ? info->version : "(null)");
    (void)data;
}

static void _core_done_cb(void *data, uint32_t id, int seq)
{
    fprintf(stderr, "[C] core done: id=%u seq=%d\n", id, seq);
    (void)data;
}

static void _core_error_relay(void *data, uint32_t id, int seq,
                              int res, const char *message)
{
    fprintf(stderr, "[C] core error: id=%u seq=%d res=%d msg=%s\n",
            id, seq, res, message ? message : "(null)");

    if (g_core_error_cb != NULL)
        g_core_error_cb(g_core_error_user_data, id, seq, res, message);
    (void)data;
}

static const struct pw_core_events g_core_events = {
    .version = PW_VERSION_CORE_EVENTS,
    .info = _core_info_cb,
    .done = _core_done_cb,
    .error = _core_error_relay,
};

static struct spa_hook g_core_hook;

void pw_bridge_set_core_error_callback(
    pw_bridge_core_error_cb_t cb, void *user_data)
{
    g_core_error_cb = cb;
    g_core_error_user_data = user_data;
}

void pw_bridge_add_core_listener(struct pw_core *core)
{
    pw_core_add_listener(core, &g_core_hook, &g_core_events, NULL);
}


/* ====================================================================
 * Binding on a node and reading its complete properties
 * ==================================================================== */

typedef void (*pw_bridge_node_info_cb_t)(
    void *user_data,
    uint32_t node_id,
    const struct spa_dict *props);

static pw_bridge_node_info_cb_t g_node_info_cb = NULL;
static void                    *g_node_info_user_data = NULL;

static void _node_info_relay(void *data,
                             const struct pw_node_info *info)
{
    if (g_node_info_cb != NULL)
        g_node_info_cb(g_node_info_user_data, info->id, info->props);
    (void)data;
}

static const struct pw_node_events g_node_events = {
    .version = PW_VERSION_NODE_EVENTS,
    .info = _node_info_relay,
};

void pw_bridge_set_node_info_callback(pw_bridge_node_info_cb_t cb,
                                      void *user_data)
{
    g_node_info_cb = cb;
    g_node_info_user_data = user_data;
}

struct pw_proxy *pw_bridge_bind_node(struct pw_registry *registry,
                                     uint32_t node_id)
{
    return (struct pw_proxy *)pw_registry_bind(
        registry, node_id, PW_TYPE_INTERFACE_Node, PW_VERSION_NODE, 0);
}

struct spa_hook *pw_bridge_node_add_listener(struct pw_proxy *proxy)
{
    struct spa_hook *hook = calloc(1, sizeof(struct spa_hook));
    if (hook == NULL)
        return NULL;
    pw_node_add_listener((struct pw_node *)proxy, hook,
                         &g_node_events, NULL);
    return hook;
}


/* ====================================================================
 * Link creation
 * ==================================================================== */

struct pw_proxy *pw_bridge_create_link(struct pw_core *core,
                                       uint32_t out_port_id,
                                       uint32_t in_port_id)
{
    char out_buf[16];
    char in_buf[16];
    struct spa_dict_item items[2];
    struct spa_dict props;

    snprintf(out_buf, sizeof(out_buf), "%u", out_port_id);
    snprintf(in_buf, sizeof(in_buf), "%u", in_port_id);

    items[0].key = "link.output.port";
    items[0].value = out_buf;
    items[1].key = "link.input.port";
    items[1].value = in_buf;

    props.flags = 0;
    props.n_items = 2;
    props.items = items;

    return pw_core_create_object(
        core,
        "link-factory",
        PW_TYPE_INTERFACE_Link,
        PW_VERSION_LINK,
        &props,
        0);
}


/* ====================================================================
 * Link destruction
 * ==================================================================== */

int pw_bridge_destroy_link(struct pw_registry *registry, uint32_t link_id)
{
    return pw_registry_destroy(registry, link_id);
}


/* ====================================================================
 * Proxy destruction
 * ==================================================================== */

void pw_bridge_destroy_proxy(struct pw_proxy *proxy)
{
    if (proxy != NULL)
        pw_proxy_destroy(proxy);
}


/* ====================================================================
 * Library version (debug)
 * ==================================================================== */

const char *pw_bridge_version(void)
{
    return pw_get_library_version();
}


/* ====================================================================
 * Thread loop helpers
 * ==================================================================== */

void pw_bridge_thread_loop_lock(void *thread_loop)
{
    pw_thread_loop_lock((struct pw_thread_loop *)thread_loop);
}

void pw_bridge_thread_loop_unlock(void *thread_loop)
{
    pw_thread_loop_unlock((struct pw_thread_loop *)thread_loop);
}