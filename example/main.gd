extends Node2D
## Draws a few colour blocks and one line of text, saves the viewport as a PNG
## and quits. Usage: godot --path example -- /absolute/output.png

func _draw() -> void:
	draw_rect(Rect2(0, 0, 640, 360), Color("1b2a41"))
	draw_rect(Rect2(40, 60, 160, 120), Color("e63946"))
	draw_rect(Rect2(240, 60, 160, 120), Color("2a9d8f"))
	draw_rect(Rect2(440, 60, 160, 120), Color("f4a261"))
	draw_string(ThemeDB.fallback_font, Vector2(40, 260), "godot_bg example: running in the background",
		HORIZONTAL_ALIGNMENT_LEFT, -1, 24, Color.WHITE)

func _ready() -> void:
	# Render at a fixed logical size so screenshots do not depend on display DPI.
	var window := get_window()
	window.content_scale_size = Vector2i(640, 360)
	window.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_KEEP
	window.content_scale_mode = Window.CONTENT_SCALE_MODE_VIEWPORT
	for i in 8:
		await get_tree().process_frame
	await RenderingServer.frame_post_draw
	var image := get_viewport().get_texture().get_image()
	var args := OS.get_cmdline_user_args()
	if args.is_empty():
		push_error("pass an output PNG path after `--`")
		get_tree().quit(2)
		return
	if image.save_png(args[0]) != OK:
		push_error("could not save " + args[0])
		get_tree().quit(1)
		return
	print("RESULT: PASS captured ", image.get_size(), " -> ", args[0])
	get_tree().quit()
