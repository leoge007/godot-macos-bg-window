// godot_bg: keeps a Godot 4 process on macOS in the background.
// Injected via DYLD_INSERT_LIBRARIES into one process only; it never modifies
// the Godot binary on disk or its rendering code paths.
#import <Cocoa/Cocoa.h>
#import <objc/runtime.h>
#include <stdio.h>
#include <unistd.h>

static IMP app_init, app_policy, window_init, window_frame, window_order;
static IMP window_alpha, window_mouse, window_sharing, window_collection;
static NSString *status_path;
static dispatch_source_t sampler;

// Writes {"active": false, "pid": ..., "error": ...} so the launcher can fail fast.
static void report_failure(NSString *path, NSString *reason) {
    fprintf(stderr, "[godot_bg] patch not applied: %s\n", reason.UTF8String);
    NSData *data = [NSJSONSerialization dataWithJSONObject:
        @{@"active": @NO, @"pid": @(getpid()), @"error": reason} options:0 error:NULL];
    [data writeToFile:path options:NSDataWritingAtomic error:NULL];
}

static BOOL replace(Class cls, SEL selector, IMP replacement, IMP *original) {
    Method method = class_getInstanceMethod(cls, selector);
    if (!method) return NO;
    if (original) *original = method_getImplementation(method);
    class_replaceMethod(cls, selector, replacement, method_getTypeEncoding(method));
    return YES;
}

static void no_activate(id self, SEL cmd) {}
static void no_activate_flag(id self, SEL cmd, BOOL flag) {}
static BOOL no_activate_options(id self, SEL cmd, NSApplicationActivationOptions options) { return NO; }
static BOOL no_key(id self, SEL cmd) { return NO; }
static NSUInteger visible(id self, SEL cmd) { return NSWindowOcclusionStateVisible; }

static BOOL prohibited(id self, SEL cmd, NSApplicationActivationPolicy policy) {
    return ((BOOL (*)(id, SEL, NSApplicationActivationPolicy))app_policy)(
        self, cmd, NSApplicationActivationPolicyProhibited);
}

static id init_app(id self, SEL cmd) {
    id app = ((id (*)(id, SEL))app_init)(self, cmd);
    if (app) prohibited(app, @selector(setActivationPolicy:), NSApplicationActivationPolicyProhibited);
    return app;
}

static NSRect offscreen(id self, SEL cmd, NSRect rect, NSScreen *screen) {
    rect.origin = NSMakePoint(-30000, -30000);
    return rect;
}

static void alpha(id self, SEL cmd, CGFloat value) {
    ((void (*)(id, SEL, CGFloat))window_alpha)(self, cmd, 0);
}
static void mouse(id self, SEL cmd, BOOL value) {
    ((void (*)(id, SEL, BOOL))window_mouse)(self, cmd, YES);
}
static void sharing(id self, SEL cmd, NSWindowSharingType value) {
    ((void (*)(id, SEL, NSWindowSharingType))window_sharing)(self, cmd, NSWindowSharingNone);
}
static void collection(id self, SEL cmd, NSWindowCollectionBehavior value) {
    value |= NSWindowCollectionBehaviorTransient | NSWindowCollectionBehaviorIgnoresCycle;
    ((void (*)(id, SEL, NSWindowCollectionBehavior))window_collection)(self, cmd, value);
}
static void frame(id self, SEL cmd, NSRect rect, BOOL display) {
    rect.origin = NSMakePoint(-30000, -30000);
    ((void (*)(id, SEL, NSRect, BOOL))window_frame)(self, cmd, rect, display);
}
static void frame_animated(id self, SEL cmd, NSRect rect, BOOL display, BOOL animate) {
    frame(self, @selector(setFrame:display:), rect, display);
}
static void origin(id self, SEL cmd, NSPoint point) {
    frame(self, @selector(setFrame:display:), [self frame], NO);
}

static void hide_window(NSWindow *window) {
    window.alphaValue = 0;
    window.ignoresMouseEvents = YES;
    window.sharingType = NSWindowSharingNone;
    window.collectionBehavior = window.collectionBehavior;
    [window setFrame:offscreen(window, NULL, window.frame, nil) display:NO];
}

// Optional window sampling: read the real NSWindow properties from inside the
// process, because non-shareable windows are missing from external window lists.
static void sample_windows(void) {
    const char *path = getenv("GODOT_BG_SAMPLE_LOG");
    if (!path || sampler) return;
    sampler = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    dispatch_source_set_timer(sampler, DISPATCH_TIME_NOW, NSEC_PER_SEC / 4, NSEC_PER_MSEC * 5);
    dispatch_source_set_event_handler(sampler, ^{
        @autoreleasepool {
            NSMutableArray *windows = [NSMutableArray array];
            for (NSWindow *window in NSApp.windows) {
                BOOL intersects = NO;
                for (NSScreen *screen in NSScreen.screens) {
                    intersects |= NSIntersectsRect(window.frame, screen.frame);
                }
                NSRect rect = window.frame;
                [windows addObject:@{@"number": @(window.windowNumber), @"x": @(rect.origin.x),
                    @"y": @(rect.origin.y), @"width": @(rect.size.width), @"height": @(rect.size.height),
                    @"alpha": @(window.alphaValue), @"ignores_mouse": @(window.ignoresMouseEvents),
                    @"sharing": @(window.sharingType), @"collection": @(window.collectionBehavior),
                    @"occlusion": @(window.occlusionState), @"intersects_screen": @(intersects)}];
            }
            NSDictionary *record = @{@"epoch": @(NSDate.date.timeIntervalSince1970), @"pid": @(getpid()),
                @"policy": @(NSApp.activationPolicy), @"windows": windows};
            NSData *data = [NSJSONSerialization dataWithJSONObject:record options:0 error:NULL];
            FILE *file = fopen(path, "a");
            if (file) { fwrite(data.bytes, 1, data.length, file); fputc('\n', file); fclose(file); }
        }
    });
    dispatch_resume(sampler);
}

static id init_window(id self, SEL cmd, NSRect rect, NSWindowStyleMask style,
        NSBackingStoreType backing, BOOL defer) {
    rect.origin = NSMakePoint(-30000, -30000);
    NSWindow *window = ((id (*)(id, SEL, NSRect, NSWindowStyleMask, NSBackingStoreType, BOOL))window_init)(
        self, cmd, rect, style, backing, defer);
    hide_window(window);
    sample_windows();
    return window;
}

static void order(id self, SEL cmd, NSWindowOrderingMode mode, NSInteger relative) {
    hide_window(self);
    ((void (*)(id, SEL, NSWindowOrderingMode, NSInteger))window_order)(self, cmd, mode, relative);
}

__attribute__((constructor)) static void install(void) {
    @autoreleasepool {
        const char *path = getenv("GODOT_BG_STATUS");
        if (!path) return; // Not requested: stay inert (e.g. the library got inserted into another process).
        status_path = [NSString stringWithUTF8String:path];
        Class app = NSClassFromString(@"GodotApplication");
        Class window = NSClassFromString(@"GodotWindow");
        if (!app || !window) {
            report_failure(status_path, @"GodotApplication / GodotWindow classes not found (not a Godot macOS build?)");
            return;
        }
        struct Hook { Class cls; SEL selector; IMP implementation; IMP *original; };
        struct Hook hooks[] = {
            {NSApplication.class, @selector(init), (IMP)init_app, &app_init},
            {NSApplication.class, @selector(setActivationPolicy:), (IMP)prohibited, &app_policy},
            {app, NSSelectorFromString(@"activateApplication"), (IMP)no_activate, NULL},
            {NSApplication.class, @selector(activateIgnoringOtherApps:), (IMP)no_activate_flag, NULL},
            {NSRunningApplication.class, @selector(activateWithOptions:), (IMP)no_activate_options, NULL},
            {NSWindow.class, @selector(initWithContentRect:styleMask:backing:defer:), (IMP)init_window, &window_init},
            {NSWindow.class, @selector(constrainFrameRect:toScreen:), (IMP)offscreen, NULL},
            {NSWindow.class, @selector(setFrame:display:), (IMP)frame, &window_frame},
            {NSWindow.class, @selector(setFrame:display:animate:), (IMP)frame_animated, NULL},
            {NSWindow.class, @selector(setFrameOrigin:), (IMP)origin, NULL},
            {NSWindow.class, @selector(setFrameTopLeftPoint:), (IMP)origin, NULL},
            {NSWindow.class, @selector(orderWindow:relativeTo:), (IMP)order, &window_order},
            {NSWindow.class, @selector(setAlphaValue:), (IMP)alpha, &window_alpha},
            {NSWindow.class, @selector(setIgnoresMouseEvents:), (IMP)mouse, &window_mouse},
            {NSWindow.class, @selector(setSharingType:), (IMP)sharing, &window_sharing},
            {NSWindow.class, @selector(setCollectionBehavior:), (IMP)collection, &window_collection},
            {window, @selector(canBecomeKeyWindow), (IMP)no_key, NULL},
            {window, @selector(canBecomeMainWindow), (IMP)no_key, NULL},
            {NSWindow.class, @selector(occlusionState), (IMP)visible, NULL},
        };
        // Check every method first, then patch: a missing selector after an OS or
        // Godot upgrade must not leave a half-applied patch.
        for (size_t i = 0; i < sizeof(hooks) / sizeof(hooks[0]); i++) {
            if (!class_getInstanceMethod(hooks[i].cls, hooks[i].selector)) {
                report_failure(status_path, [NSString stringWithFormat:
                    @"missing selector %s", sel_getName(hooks[i].selector)]);
                return;
            }
        }
        for (size_t i = 0; i < sizeof(hooks) / sizeof(hooks[0]); i++) {
            replace(hooks[i].cls, hooks[i].selector, hooks[i].implementation, hooks[i].original);
        }
        // Older macOS has no -activate; a method that does not exist cannot steal focus.
        replace(NSApplication.class, NSSelectorFromString(@"activate"), (IMP)no_activate, NULL);
        NSData *status = [NSJSONSerialization dataWithJSONObject:
            @{@"active": @YES, @"pid": @(getpid())} options:0 error:NULL];
        if ([status writeToFile:status_path options:NSDataWritingAtomic error:NULL]) {
            fprintf(stderr, "[godot_bg] background patch loaded, pid=%d\n", getpid());
        }
    }
}
