#include <gtk/gtk.h>
#include <gtk-layer-shell.h>

typedef struct {
  GtkWidget *label;
  GtkWidget *clock_label;
  GtkWidget *volume_box;
  GtkWidget *volume_bar;
  GtkWidget *progress;
  gchar *path;
  gchar *last_text;
  gchar *last_clock_text;
  gdouble last_fraction;
  gdouble last_volume;
  gboolean pulse;
  gboolean fullscreen;
} AppState;

static gchar *read_popup_text(const gchar *path, gdouble *fraction, gdouble *volume, gboolean *pulse) {
  gchar *contents = NULL;
  gsize length = 0;
  GError *error = NULL;
  *fraction = -1.0;
  *volume = -1.0;
  *pulse = FALSE;

  if (!g_file_get_contents(path, &contents, &length, &error)) {
    if (error != NULL) {
      g_error_free(error);
    }
    return g_strdup("录音中");
  }

  gchar **lines = g_strsplit(contents, "\n", 0);
  GString *message = g_string_new(NULL);
  for (guint i = 0; lines[i] != NULL; i++) {
    gchar *line = g_strstrip(lines[i]);
    if (g_str_has_prefix(line, "@progress=")) {
      gchar *end = NULL;
      gdouble value = g_ascii_strtod(line + 10, &end);
      if (end != line + 10) {
        *fraction = CLAMP(value, 0.0, 1.0);
      }
      continue;
    }
    if (g_str_has_prefix(line, "@volume=")) {
      gchar *end = NULL;
      gdouble value = g_ascii_strtod(line + 8, &end);
      if (end != line + 8) {
        *volume = CLAMP(value, 0.0, 1.0);
      }
      continue;
    }
    if (g_str_has_prefix(line, "@pulse=")) {
      gchar *value = g_strstrip(line + 7);
      *pulse = g_strcmp0(value, "1") == 0 ||
               g_ascii_strcasecmp(value, "true") == 0 ||
               g_ascii_strcasecmp(value, "on") == 0;
      continue;
    }
    if (g_str_has_prefix(line, "#")) {
      line = g_strstrip(line + 1);
    }
    if (*line == '\0') {
      continue;
    }
    if (message->len > 0) {
      g_string_append_c(message, '\n');
    }
    g_string_append(message, line);
  }
  if (message->len == 0) {
    g_string_free(message, TRUE);
    g_strfreev(lines);
    g_free(contents);
    return g_strdup("录音中");
  }

  gchar *result = g_string_free(message, FALSE);
  g_strfreev(lines);
  g_free(contents);
  return result;
}

static gboolean refresh_label(gpointer user_data) {
  AppState *state = (AppState *)user_data;
  gdouble fraction = -1.0;
  gdouble volume = -1.0;
  gboolean pulse = FALSE;
  gchar *text = read_popup_text(state->path, &fraction, &volume, &pulse);
  gchar *clock_text = NULL;

  if (state->last_text == NULL || g_strcmp0(text, state->last_text) != 0) {
    gtk_label_set_text(GTK_LABEL(state->label), text);
    g_free(state->last_text);
    state->last_text = g_strdup(text);
  }
  if (state->fullscreen && state->clock_label != NULL) {
    GDateTime *now = g_date_time_new_now_local();
    clock_text = g_date_time_format(now, "%H:%M");
    g_date_time_unref(now);
    if (state->last_clock_text == NULL || g_strcmp0(clock_text, state->last_clock_text) != 0) {
      gtk_label_set_text(GTK_LABEL(state->clock_label), clock_text);
      g_free(state->last_clock_text);
      state->last_clock_text = g_strdup(clock_text);
    }
  }
  if (pulse) {
    gtk_widget_show(state->progress);
    gtk_progress_bar_pulse(GTK_PROGRESS_BAR(state->progress));
    state->pulse = TRUE;
    state->last_fraction = -1.0;
  } else if (fraction >= 0.0) {
    gtk_widget_show(state->progress);
    if (state->pulse) {
      gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(state->progress), fraction);
      state->pulse = FALSE;
      state->last_fraction = fraction;
    }
    if (fraction != state->last_fraction) {
      gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(state->progress), fraction);
      state->last_fraction = fraction;
    }
  } else {
    gtk_widget_hide(state->progress);
    state->pulse = FALSE;
    state->last_fraction = -1.0;
  }
  if (!state->fullscreen && state->volume_box != NULL && state->volume_bar != NULL && !pulse) {
    gtk_widget_show_all(state->volume_box);
    gdouble visible_volume = volume >= 0.0 ? volume : 0.0;
    if (visible_volume != state->last_volume) {
      gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(state->volume_bar), visible_volume);
      state->last_volume = visible_volume;
    }
  } else if (state->volume_box != NULL) {
    gtk_widget_hide(state->volume_box);
    state->last_volume = -1.0;
  }

  g_free(clock_text);
  g_free(text);
  return G_SOURCE_CONTINUE;
}

static void app_state_free(gpointer user_data) {
  AppState *state = (AppState *)user_data;
  if (state == NULL) {
    return;
  }
  g_free(state->path);
  g_free(state->last_text);
  g_free(state->last_clock_text);
  g_free(state);
}

static void install_dark_theme(void) {
  GtkCssProvider *provider = gtk_css_provider_new();
  const gchar *css =
      ".uconsole-asr-window {"
      "  background: #111318;"
      "}"
      ".uconsole-asr-box {"
      "  background: #111318;"
      "}"
      ".uconsole-asr-label {"
      "  color: #f3f6fb;"
      "  text-shadow: none;"
      "}"
      ".uconsole-asr-progress {"
      "  min-height: 8px;"
      "}"
      ".uconsole-asr-volume-icon {"
      "  color: #f3f6fb;"
      "}"
      ".uconsole-asr-volume {"
      "  min-height: 6px;"
      "}"
      ".uconsole-asr-volume trough {"
      "  background: #2a303a;"
      "  border-radius: 3px;"
      "}"
      ".uconsole-asr-volume progress {"
      "  background: #41d399;"
      "  border-radius: 3px;"
      "}"
      ".uconsole-asr-progress trough {"
      "  background: #2a303a;"
      "  border-radius: 4px;"
      "}"
      ".uconsole-asr-progress progress {"
      "  background: #6ea8fe;"
      "  border-radius: 4px;"
      "}"
      ".uconsole-lock-window {"
      "  background: #05070a;"
      "}"
      ".uconsole-lock-box {"
      "  background: #05070a;"
      "}"
      ".uconsole-lock-label {"
      "  color: #f8fafc;"
      "}"
      ".uconsole-lock-clock {"
      "  color: #f8fafc;"
      "}"
      ".uconsole-lock-progress {"
      "  min-height: 12px;"
      "}"
      ".uconsole-lock-progress trough {"
      "  background: #1d2430;"
      "  border-radius: 6px;"
      "}"
      ".uconsole-lock-progress progress {"
      "  background: #8ab4ff;"
      "  border-radius: 6px;"
      "}";

  gtk_css_provider_load_from_data(provider, css, -1, NULL);
  gtk_style_context_add_provider_for_screen(
      gdk_screen_get_default(),
      GTK_STYLE_PROVIDER(provider),
      GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
  g_object_unref(provider);
}

int main(int argc, char **argv) {
  gtk_init(&argc, &argv);

  if (argc < 2) {
    g_printerr("usage: uconsole-asr-popup <text-file>\n");
    return 2;
  }

  install_dark_theme();
  gboolean fullscreen = g_str_has_suffix(argv[1], "uconsole-helper-lock-popup.txt");

  GtkWidget *window = gtk_window_new(GTK_WINDOW_POPUP);
  gtk_style_context_add_class(
      gtk_widget_get_style_context(window),
      fullscreen ? "uconsole-lock-window" : "uconsole-asr-window");
  gtk_window_set_title(GTK_WINDOW(window), "");
  gtk_window_set_default_size(GTK_WINDOW(window), fullscreen ? 1280 : 460, fullscreen ? 720 : 150);
  gtk_window_set_keep_above(GTK_WINDOW(window), TRUE);
  gtk_window_set_decorated(GTK_WINDOW(window), FALSE);
  gtk_window_set_resizable(GTK_WINDOW(window), FALSE);
  gtk_window_set_skip_taskbar_hint(GTK_WINDOW(window), TRUE);
  gtk_window_set_skip_pager_hint(GTK_WINDOW(window), TRUE);
  gtk_layer_init_for_window(GTK_WINDOW(window));
  gtk_layer_set_layer(GTK_WINDOW(window), GTK_LAYER_SHELL_LAYER_OVERLAY);
  if (fullscreen) {
    gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_TOP, TRUE);
    gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_BOTTOM, TRUE);
    gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_LEFT, TRUE);
    gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_RIGHT, TRUE);
    gtk_layer_set_exclusive_zone(GTK_WINDOW(window), -1);
  } else {
    gtk_layer_set_anchor(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_TOP, TRUE);
    gtk_layer_set_margin(GTK_WINDOW(window), GTK_LAYER_SHELL_EDGE_TOP, 80);
  }
  gtk_layer_set_keyboard_mode(GTK_WINDOW(window), GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);

  GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
  gtk_style_context_add_class(
      gtk_widget_get_style_context(box),
      fullscreen ? "uconsole-lock-box" : "uconsole-asr-box");
  gtk_widget_set_margin_top(box, fullscreen ? 0 : 22);
  gtk_widget_set_margin_bottom(box, fullscreen ? 0 : 22);
  gtk_widget_set_margin_start(box, fullscreen ? 72 : 14);
  gtk_widget_set_margin_end(box, fullscreen ? 72 : 14);
  if (fullscreen) {
    gtk_widget_set_valign(box, GTK_ALIGN_CENTER);
  }
  gtk_container_add(GTK_CONTAINER(window), box);

  GtkWidget *clock_label = NULL;
  if (fullscreen) {
    clock_label = gtk_label_new("");
    gtk_style_context_add_class(
        gtk_widget_get_style_context(clock_label),
        "uconsole-lock-clock");
    gtk_label_set_justify(GTK_LABEL(clock_label), GTK_JUSTIFY_CENTER);
    gtk_label_set_xalign(GTK_LABEL(clock_label), 0.5);
    gtk_widget_set_halign(clock_label, GTK_ALIGN_CENTER);
    gtk_widget_set_valign(clock_label, GTK_ALIGN_CENTER);
    gtk_widget_set_margin_bottom(clock_label, 28);
    gtk_box_pack_start(GTK_BOX(box), clock_label, FALSE, FALSE, 0);

    PangoAttrList *clock_attrs = pango_attr_list_new();
    pango_attr_list_insert(clock_attrs, pango_attr_scale_new(5.0));
    pango_attr_list_insert(clock_attrs, pango_attr_weight_new(PANGO_WEIGHT_LIGHT));
    gtk_label_set_attributes(GTK_LABEL(clock_label), clock_attrs);
    pango_attr_list_unref(clock_attrs);
  }

  GtkWidget *label = gtk_label_new("录音中");
  gtk_style_context_add_class(
      gtk_widget_get_style_context(label),
      fullscreen ? "uconsole-lock-label" : "uconsole-asr-label");
  gtk_label_set_line_wrap(GTK_LABEL(label), TRUE);
  gtk_label_set_line_wrap_mode(GTK_LABEL(label), PANGO_WRAP_WORD_CHAR);
  gtk_label_set_max_width_chars(GTK_LABEL(label), fullscreen ? 36 : 30);
  gtk_label_set_justify(GTK_LABEL(label), GTK_JUSTIFY_CENTER);
  gtk_label_set_xalign(GTK_LABEL(label), 0.5);
  gtk_label_set_yalign(GTK_LABEL(label), 0.5);
  gtk_widget_set_halign(label, GTK_ALIGN_CENTER);
  gtk_widget_set_valign(label, GTK_ALIGN_CENTER);
  gtk_box_pack_start(GTK_BOX(box), label, TRUE, TRUE, 0);

  GtkWidget *volume_box = NULL;
  GtkWidget *volume_bar = NULL;
  if (!fullscreen) {
    volume_box = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 10);
    gtk_widget_set_margin_top(volume_box, 12);
    gtk_widget_set_margin_start(volume_box, 26);
    gtk_widget_set_margin_end(volume_box, 26);
    gtk_box_pack_start(GTK_BOX(box), volume_box, FALSE, FALSE, 0);

    GtkWidget *volume_icon = gtk_image_new_from_icon_name("audio-input-microphone-symbolic", GTK_ICON_SIZE_BUTTON);
    gtk_style_context_add_class(
        gtk_widget_get_style_context(volume_icon),
        "uconsole-asr-volume-icon");
    gtk_widget_set_halign(volume_icon, GTK_ALIGN_CENTER);
    gtk_widget_set_valign(volume_icon, GTK_ALIGN_CENTER);
    gtk_box_pack_start(GTK_BOX(volume_box), volume_icon, FALSE, FALSE, 0);

    volume_bar = gtk_progress_bar_new();
    gtk_style_context_add_class(
        gtk_widget_get_style_context(volume_bar),
        "uconsole-asr-volume");
    gtk_widget_set_hexpand(volume_bar, TRUE);
    gtk_widget_set_valign(volume_bar, GTK_ALIGN_CENTER);
    gtk_box_pack_start(GTK_BOX(volume_box), volume_bar, TRUE, TRUE, 0);
  }

  GtkWidget *progress = gtk_progress_bar_new();
  gtk_style_context_add_class(
      gtk_widget_get_style_context(progress),
      fullscreen ? "uconsole-lock-progress" : "uconsole-asr-progress");
  gtk_widget_set_margin_top(progress, fullscreen ? 24 : 14);
  gtk_widget_set_margin_start(progress, fullscreen ? 120 : 24);
  gtk_widget_set_margin_end(progress, fullscreen ? 120 : 24);
  gtk_box_pack_start(GTK_BOX(box), progress, FALSE, FALSE, 0);
  gtk_widget_set_no_show_all(progress, TRUE);
  gtk_widget_hide(progress);

  PangoAttrList *attrs = pango_attr_list_new();
  pango_attr_list_insert(attrs, pango_attr_scale_new(fullscreen ? 2.2 : 1.45));
  gtk_label_set_attributes(GTK_LABEL(label), attrs);
  pango_attr_list_unref(attrs);

  AppState *state = g_new0(AppState, 1);
  state->label = label;
  state->clock_label = clock_label;
  state->volume_box = volume_box;
  state->volume_bar = volume_bar;
  state->progress = progress;
  state->path = g_strdup(argv[1]);
  state->last_fraction = -1.0;
  state->last_volume = -1.0;
  state->fullscreen = fullscreen;
  g_object_set_data_full(G_OBJECT(window), "app-state", state, app_state_free);

  g_signal_connect(window, "destroy", G_CALLBACK(gtk_main_quit), NULL);
  refresh_label(state);
  g_timeout_add(40, refresh_label, state);

  gtk_widget_show_all(window);
  if (state->last_fraction < 0.0) {
    gtk_widget_hide(progress);
  }
  gtk_window_present(GTK_WINDOW(window));
  gtk_main();
  return 0;
}
