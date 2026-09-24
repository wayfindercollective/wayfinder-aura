#import "wf_render_clock.h"

// One render thread per process; it only ever runs display links.
static NSRunLoop *WFRenderRunLoop(void) {
    static NSRunLoop *loop = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        dispatch_semaphore_t ready = dispatch_semaphore_create(0);
        __block NSRunLoop *captured = nil;
        NSThread *thread = [[NSThread alloc] initWithBlock:^{
            @autoreleasepool {
                captured = NSRunLoop.currentRunLoop;
                // A port keeps the loop alive (and asleep) with no links running.
                [captured addPort:[NSMachPort port] forMode:NSDefaultRunLoopMode];
            }
            dispatch_semaphore_signal(ready);
            while (YES) {
                @autoreleasepool {
                    [NSRunLoop.currentRunLoop runMode:NSDefaultRunLoopMode
                                           beforeDate:NSDate.distantFuture];
                }
            }
        }];
        thread.name = @"Wayfinder waves";
        thread.qualityOfService = NSQualityOfServiceUserInteractive;
        [thread start];
        dispatch_semaphore_wait(ready, DISPATCH_TIME_FOREVER);
        loop = captured;
    });
    return loop;
}

static NSView *WFViewForLayer(CALayer *layer) {
    for (CALayer *current = layer; current != nil; current = current.superlayer) {
        id delegate = current.delegate;
        if ([delegate isKindOfClass:NSView.class]) return (NSView *)delegate;
    }
    return nil;
}

// The link retains its target; a weak trampoline avoids a renderer <-> link cycle.
@interface WFRenderClockTrampoline : NSObject
@property(nonatomic, weak) id<WFRenderClockTarget> target;
@end

@implementation WFRenderClockTrampoline
- (void)tick:(CADisplayLink *)link {
    id<WFRenderClockTarget> target = self.target;  // strong for this frame
    if (target == nil) return;
    @autoreleasepool {
        [target renderClockTickAt:link.targetTimestamp];
    }
}
@end

@implementation WFRenderClock {
    CADisplayLink *_link;
    WFRenderClockTrampoline *_trampoline;
    BOOL _running;
}

- (instancetype)initWithLayer:(CALayer *)layer
                       target:(id<WFRenderClockTarget>)target
                 preferredFPS:(float)fps {
    self = [super init];
    if (!self) return nil;
    _trampoline = [WFRenderClockTrampoline new];
    _trampoline.target = target;
    // A view's link follows the screen its window is on (refresh rate included).
    NSView *view = WFViewForLayer(layer);
    NSScreen *screen = view.window.screen ?: NSScreen.mainScreen;
    if (view != nil) {
        _link = [view displayLinkWithTarget:_trampoline selector:@selector(tick:)];
    } else if (screen != nil) {
        _link = [screen displayLinkWithTarget:_trampoline selector:@selector(tick:)];
    }
    if (_link == nil) return nil;
    _link.preferredFrameRateRange = CAFrameRateRangeMake(fps * 0.5f, fps, fps);
    _link.paused = YES;
    [_link addToRunLoop:WFRenderRunLoop() forMode:NSRunLoopCommonModes];
    return self;
}

- (BOOL)running {
    return _running;
}

- (void)start {
    if (_link == nil || _running) return;
    _running = YES;
    _link.paused = NO;
}

- (void)stop {
    if (_link == nil || !_running) return;
    _running = NO;
    _link.paused = YES;
}

- (void)invalidate {
    _running = NO;
    [_link invalidate];
    _link = nil;
    _trampoline.target = nil;
}

- (void)dealloc {
    [_link invalidate];
}

@end
