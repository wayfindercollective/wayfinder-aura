#import <Cocoa/Cocoa.h>
#import <QuartzCore/QuartzCore.h>

NS_ASSUME_NONNULL_BEGIN

/// The object a WFRenderClock drives. Called on the private render thread.
@protocol WFRenderClockTarget <NSObject>
/// ``targetTimestamp`` is when this frame will reach the display; animate to
/// it (not to "now") so motion lands evenly on every refresh.
- (void)renderClockTickAt:(CFTimeInterval)targetTimestamp;
@end

/// Display-synced frame callbacks on a private render thread.
///
/// The Metal waves used an NSTimer on the main run loop: it is not aligned to
/// the display (frames land on uneven refreshes, which reads as judder) and
/// it skips frames whenever the main thread is busy (Tk/Python or Qt work).
/// A CADisplayLink from the layer's view, scheduled on a dedicated thread,
/// fixes both. A paused clock does no work and the thread sleeps in the
/// kernel, so idle cost is zero.
@interface WFRenderClock : NSObject
- (nullable instancetype)initWithLayer:(CALayer *)layer
                                target:(id<WFRenderClockTarget>)target
                          preferredFPS:(float)fps;
@property(nonatomic, readonly) BOOL running;
- (void)start;
- (void)stop;
- (void)invalidate;
@end

NS_ASSUME_NONNULL_END
