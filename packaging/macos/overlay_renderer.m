#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#import <simd/simd.h>

typedef struct {
    vector_float4 dims;    // logical width, height, wave time, breath time
    vector_float4 params;  // audio level, backing scale, unused, unused
    vector_float4 color;
} WFOverlayUniforms;

@interface WFOverlayRenderer : NSObject
@property(nonatomic, strong) CAMetalLayer *metalLayer;
@property(nonatomic, strong) id<MTLCommandQueue> commandQueue;
@property(nonatomic, strong) id<MTLComputePipelineState> pipeline;
@property(nonatomic, strong) NSTimer *timer;
@property(nonatomic) CGFloat backingScale;
@property(nonatomic) int width;
@property(nonatomic) int height;
@property(nonatomic) float waveTime;
@property(nonatomic) float breathTime;
@property(nonatomic) float audioLevel;
@property(nonatomic) vector_float4 color;
@property(nonatomic) CFTimeInterval lastFrameTime;
@property(nonatomic) BOOL stopped;
@property(nonatomic) BOOL idle;
@property(nonatomic) BOOL settled;
@property(nonatomic) CFTimeInterval idleSince;
@end

// Idle READY pill: after this long with nothing happening, stop issuing Metal
// work and let Core Animation breathe the last frame. Any Metal client that
// presents continuously keeps ~256 MB of GPU memory and a 15 fps wakeup alive
// (measured on M3 Ultra / macOS 27); the render server runs the breath for
// free. Any state change, level, resize or reveal wakes the wave instantly.
static const CFTimeInterval kWFSettleAfterSeconds = 20.0;
static NSString *const kWFBreathKey = @"WayfinderIdleBreath";

@implementation WFOverlayRenderer

- (instancetype)initWithParentLayer:(CALayer *)parentLayer
                       backingScale:(CGFloat)backingScale
                       shaderSource:(NSString *)shaderSource {
    self = [super init];
    if (!self) return nil;
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device) return nil;
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:shaderSource options:nil error:&error];
    if (!library) {
        NSLog(@"Wayfinder overlay Metal compile failed: %@", error);
        return nil;
    }
    id<MTLFunction> function = [library newFunctionWithName:@"overlay_wave"];
    _pipeline = [device newComputePipelineStateWithFunction:function error:&error];
    if (!_pipeline) {
        NSLog(@"Wayfinder overlay Metal pipeline failed: %@", error);
        return nil;
    }
    _commandQueue = [device newCommandQueue];
    if (!_commandQueue) return nil;

    _backingScale = fmax(1.0, backingScale);
    _metalLayer = [CAMetalLayer layer];
    _metalLayer.name = @"WayfinderOverlayWaveformMetal";
    _metalLayer.device = device;
    _metalLayer.pixelFormat = MTLPixelFormatBGRA8Unorm;
    // The compute kernel writes the drawable, which needs ShaderWrite usage.
    // framebufferOnly=YES gives render-target-only textures; on M3 the
    // invalid write shows up as solid magenta.
    _metalLayer.framebufferOnly = NO;
    // Tag the drawable as sRGB so its colours match Tk's sRGB surfaces; an
    // untagged layer is shown in the display's native space and the hero
    // strip reads as a differently tinted rectangle on its card.
    CGColorSpaceRef srgb = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    _metalLayer.colorspace = srgb;
    CGColorSpaceRelease(srgb);
    _metalLayer.displaySyncEnabled = YES;
    _metalLayer.presentsWithTransaction = NO;
    _metalLayer.maximumDrawableCount = 3;
    _metalLayer.opaque = NO;
    _metalLayer.backgroundColor = NSColor.clearColor.CGColor;
    _metalLayer.masksToBounds = YES;
    _metalLayer.contentsScale = _backingScale;
    _metalLayer.zPosition = 1100.0;
    [parentLayer addSublayer:_metalLayer];

    _lastFrameTime = CACurrentMediaTime();
    _idleSince = _lastFrameTime;
    _color = (vector_float4){91.0f / 255.0f, 143.0f / 255.0f, 212.0f / 255.0f, 1.0f};
    [self startTimer];
    return self;
}

- (void)startTimer {
    if (_timer || _stopped) return;
    _timer = [NSTimer timerWithTimeInterval:(1.0 / 15.0)
                                     target:self
                                   selector:@selector(drawFrame:)
                                   userInfo:nil
                                    repeats:YES];
    [[NSRunLoop mainRunLoop] addTimer:_timer forMode:NSRunLoopCommonModes];
}

- (void)settle {
    if (_settled || _stopped) return;
    _settled = YES;
    [_timer invalidate];
    _timer = nil;
    CABasicAnimation *breath = [CABasicAnimation animationWithKeyPath:@"opacity"];
    breath.fromValue = @1.0;
    breath.toValue = @0.7;
    breath.duration = 2.4;
    breath.autoreverses = YES;
    breath.repeatCount = HUGE_VALF;
    breath.timingFunction =
        [CAMediaTimingFunction functionWithName:kCAMediaTimingFunctionEaseInEaseOut];
    [_metalLayer addAnimation:breath forKey:kWFBreathKey];
}

- (void)wake {
    _idleSince = CACurrentMediaTime();
    if (!_settled || _stopped) return;
    _settled = NO;
    [_metalLayer removeAnimationForKey:kWFBreathKey];
    _lastFrameTime = CACurrentMediaTime();
    [self startTimer];
}

- (void)setIdleState:(BOOL)idle {
    if (idle == _idle) return;
    _idle = idle;
    [self wake];
}

- (void)drawFrame:(NSTimer *)timer {
    (void)timer;
    if (_stopped || _metalLayer.hidden || _width <= 1 || _height <= 1) return;
    @autoreleasepool {
        CFTimeInterval now = CACurrentMediaTime();
        float dt = (float)fmin(fmax(now - _lastFrameTime, 0.0), 0.1);
        _lastFrameTime = now;
        _waveTime += dt * 3.0f;
        _breathTime += dt * 0.5f;

        id<CAMetalDrawable> drawable = [_metalLayer nextDrawable];
        if (!drawable) return;
        WFOverlayUniforms uniforms = {
            .dims = (vector_float4){(float)_width, (float)_height, _waveTime, _breathTime},
            // Match the Qt path renderer: the wave layout box has 4px of
            // vertical breathing room before the pill clips its outer glow.
            .params = (vector_float4){_audioLevel, (float)_backingScale, 4.0f, 0.0f},
            .color = _color,
        };
        id<MTLCommandBuffer> commandBuffer = [_commandQueue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:_pipeline];
        [encoder setTexture:drawable.texture atIndex:0];
        [encoder setBytes:&uniforms length:sizeof(uniforms) atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(drawable.texture.width, drawable.texture.height, 1)
            threadsPerThreadgroup:MTLSizeMake(16, 8, 1)];
        [encoder endEncoding];
        [commandBuffer presentDrawable:drawable];
        [commandBuffer commit];

        if (_idle && _audioLevel < 0.01f && now - _idleSince >= kWFSettleAfterSeconds) {
            [self settle];  // this frame stays on screen; CA breathes it
        }
    }
}

- (void)setFrameX:(double)x y:(double)y width:(int)width height:(int)height {
    if (width != _width || height != _height) [self wake];
    _width = width;
    _height = height;
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.frame = CGRectMake(x, y, width, height);
    _metalLayer.drawableSize = CGSizeMake(
        ceil(width * _backingScale), ceil(height * _backingScale)
    );
    [CATransaction commit];
}

- (void)setAudioLevel:(float)audioLevel color:(vector_float4)color {
    _audioLevel = fmaxf(0.0f, fminf(audioLevel, 1.0f));
    if (_audioLevel >= 0.01f || !simd_equal(color, _color)) [self wake];
    _color = color;
}

- (void)setRendererHidden:(BOOL)hidden {
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.hidden = hidden;
    [CATransaction commit];
    _lastFrameTime = CACurrentMediaTime();
    if (!hidden) [self wake];
}

- (void)stop {
    _stopped = YES;
    [_timer invalidate];
    _timer = nil;
    [_metalLayer removeFromSuperlayer];
}

@end

void *wf_overlay_create(void *parentLayerPtr, double backingScale, const char *shaderUTF8) {
    if (!parentLayerPtr || !shaderUTF8) return NULL;
    CALayer *parentLayer = (__bridge CALayer *)parentLayerPtr;
    NSString *shader = [NSString stringWithUTF8String:shaderUTF8];
    WFOverlayRenderer *renderer = [[WFOverlayRenderer alloc]
        initWithParentLayer:parentLayer backingScale:backingScale shaderSource:shader];
    return renderer ? (__bridge_retained void *)renderer : NULL;
}

void wf_overlay_set_frame(void *handle, double x, double y, int width, int height) {
    if (!handle) return;
    [(__bridge WFOverlayRenderer *)handle setFrameX:x y:y width:width height:height];
}

void wf_overlay_set_state(void *handle, float audioLevel, float red, float green, float blue) {
    if (!handle) return;
    [(__bridge WFOverlayRenderer *)handle
        setAudioLevel:audioLevel color:(vector_float4){red, green, blue, 1.0f}];
}

void wf_overlay_set_idle(void *handle, int idle) {
    if (!handle) return;
    [(__bridge WFOverlayRenderer *)handle setIdleState:(idle != 0)];
}

void wf_overlay_set_hidden(void *handle, int hidden) {
    if (!handle) return;
    [(__bridge WFOverlayRenderer *)handle setRendererHidden:(hidden != 0)];
}

void wf_overlay_destroy(void *handle) {
    if (!handle) return;
    WFOverlayRenderer *renderer = (__bridge_transfer WFOverlayRenderer *)handle;
    [renderer stop];
}
