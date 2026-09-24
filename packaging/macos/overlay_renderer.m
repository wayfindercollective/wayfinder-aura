#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#import <os/lock.h>
#import <simd/simd.h>

#import "wf_render_clock.h"

typedef struct {
    vector_float4 dims;    // logical width, height, wave time, breath time
    vector_float4 params;  // audio level, backing scale, unused, unused
    vector_float4 color;
} WFOverlayUniforms;

// ---------------------------------------------------------------------------
// Idle ("READY") waves run on Core Animation, not Metal.
//
// Any Metal client that presents continuously keeps ~256 MB of GPU memory and
// a timer wakeup alive (measured on M3 Ultra / macOS 27). The idle pill is on
// screen all day, so after a short idle the same wave is handed to vector
// CAShapeLayers whose paths are animated by the render server: the app does no
// per-frame work, holds no Metal memory, and the motion runs at the display's
// refresh rate.
//
// The vector strands evaluate the same functions as the Metal shader
// (src/wayfinder/ui/macos_overlay_metal.py). Every time rate is chosen so each
// term completes whole cycles in kWFLoopSeconds, which makes the keyframe loop
// seamless; Metal uses the same rates, so hand-offs in both directions keep
// phase.
// ---------------------------------------------------------------------------
static const float kWFWaveRate = 3.0f;      // wave-time units per second
static const float kWFBreathRate = 0.6f;    // breath radians per second
static const double kWFLoopSeconds = 10.0 * M_PI / 3.0;  // 10.47 s: 5/8/10 + 7/10 cycles, 1 breath
static const int kWFKeyframesPerSecond = 30;
static const CFTimeInterval kWFVectorAfterSeconds = 1.0;  // let state transitions finish
static const CFTimeInterval kWFCrossfadeSeconds = 0.3;
static const float kWFBleed = 4.0f;         // params.z in the Metal path
static const float kWFFramesPerSecond = 60.0f;  // Metal (speaking) frames, display-synced
// Voice level easing (seconds): quick to rise, gentler to fall, so the wave
// follows speech without stepping between the ~20 Hz level updates.
static const float kWFLevelAttack = 0.045f;
static const float kWFLevelRelease = 0.14f;

static const float kWFFreqs[4] = {0.07f, 0.11f, 0.16f, 0.22f};
static const float kWFPhases[4] = {0.0f, 1.0f, 2.2f, 0.7f};
static const float kWFAlphas[4] = {0.15f, 0.25f, 0.40f, 0.55f};
static const float kWFThickness[4] = {6.0f, 5.0f, 4.0f, 3.0f};
static NSString *const kWFFlowKey = @"wfFlow";

static float WFWaveY(float x, float center, float amp, float freq, float phase, float t, float height) {
    float y = center + amp * sinf(freq * x + t + phase);
    y += amp * 0.4f * sinf(freq * 2.3f * x + t * 1.6f + phase);
    y += amp * 0.2f * sinf(freq * 3.7f * x + t * 2.0f + phase * 0.5f);
    return fminf(fmaxf(y, 0.0f), height);
}

static float WFHighlightY(float x, float center, float amp, float t, float height) {
    float y = center + amp * sinf(0.13f * x + t * 1.4f);
    y += amp * 0.5f * sinf(0.26f * x + t * 2.0f + 0.8f);
    return fminf(fmaxf(y, 0.0f), height);
}

// Strand 0-3: the four waves; 4: the highlight line. Idle amplitude (level 0).
static CGPathRef WFCreateStrandPath(int strand, float width, float height, float t, float breathTime) {
    float center = height * 0.5f;
    float maxAmp = height * 0.4f;
    float baseBreath = 0.15f + 0.12f * (0.5f + 0.5f * sinf(breathTime));
    float amp = maxAmp * baseBreath;
    CGMutablePathRef path = CGPathCreateMutable();
    int steps = MAX(2, (int)ceilf(width / 2.0f));
    for (int i = 0; i <= steps; ++i) {
        float x = fminf(width, 2.0f * (float)i);
        float y = strand < 4
            ? WFWaveY(x, center, amp, kWFFreqs[strand], kWFPhases[strand], t, height)
            : WFHighlightY(x, center, amp, t, height);
        if (i == 0) {
            CGPathMoveToPoint(path, NULL, x + kWFBleed, y + kWFBleed);
        } else {
            CGPathAddLineToPoint(path, NULL, x + kWFBleed, y + kWFBleed);
        }
    }
    return path;
}

@interface WFOverlayRenderer : NSObject <WFRenderClockTarget>
@property(nonatomic, strong) CAMetalLayer *metalLayer;
@property(nonatomic, strong) id<MTLCommandQueue> commandQueue;
@property(nonatomic, strong) id<MTLComputePipelineState> pipeline;
@property(nonatomic, strong) WFRenderClock *clock;
@property(nonatomic) CGFloat backingScale;
@property(nonatomic) int width;
@property(nonatomic) int height;
@property(nonatomic) float waveTime;
@property(nonatomic) float audioLevel;        // eased, what is drawn
@property(nonatomic) float targetAudioLevel;  // latest from Qt
@property(nonatomic) vector_float4 color;
@property(nonatomic) CFTimeInterval lastFrameTime;
@property(nonatomic) BOOL stopped;
@property(nonatomic) BOOL idle;
@property(nonatomic) BOOL rendererHidden;
@property(nonatomic) CFTimeInterval idleSince;
// Vector idle
@property(nonatomic, strong) CALayer *vectorLayer;
@property(nonatomic, strong) NSArray<CAShapeLayer *> *vectorStrokes;
@property(nonatomic) int vectorWidth;
@property(nonatomic) int vectorHeight;
@property(nonatomic) BOOL vectorActive;
@property(nonatomic) CFTimeInterval vectorStartTime;
@property(nonatomic) double vectorStartLoopPos;
@property(nonatomic) NSUInteger vectorGeneration;
@property(nonatomic) BOOL settleRequested;
@end

// Threading: the clock ticks on the render thread; everything else runs on
// the main (Qt) thread. _lock guards the state both touch and is never held
// across Metal calls.
@implementation WFOverlayRenderer {
    os_unfair_lock _lock;
}

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

    _vectorLayer = [CALayer layer];
    _vectorLayer.name = @"WayfinderOverlayWaveformVector";
    _vectorLayer.geometryFlipped = YES;  // y grows downward, as in the shader
    _vectorLayer.masksToBounds = YES;
    _vectorLayer.contentsScale = _backingScale;
    _vectorLayer.zPosition = 1101.0;
    _vectorLayer.opacity = 0.0f;
    _vectorLayer.hidden = YES;
    [parentLayer addSublayer:_vectorLayer];

    _lock = OS_UNFAIR_LOCK_INIT;
    _clock = [[WFRenderClock alloc] initWithLayer:parentLayer
                                           target:self
                                     preferredFPS:kWFFramesPerSecond];
    if (!_clock) {
        [_metalLayer removeFromSuperlayer];
        [_vectorLayer removeFromSuperlayer];
        return nil;
    }
    _lastFrameTime = CACurrentMediaTime();
    _idleSince = _lastFrameTime;
    _color = (vector_float4){91.0f / 255.0f, 143.0f / 255.0f, 212.0f / 255.0f, 1.0f};
    [self startTimer];
    return self;
}

- (float)breathTime {
    // Tied to wave time, so Metal and the vector loop always agree on phase.
    return _waveTime * (kWFBreathRate / kWFWaveRate);
}

- (void)startTimer {
    if (_stopped || _clock.running) return;
    os_unfair_lock_lock(&_lock);
    _lastFrameTime = CACurrentMediaTime();
    os_unfair_lock_unlock(&_lock);
    [_clock start];
}

- (void)stopTimer {
    [_clock stop];
}

// ---------------- vector idle ----------------

- (CGColorRef)createColorWithAlpha:(float)alpha {
    return CGColorCreateSRGB(_color.x, _color.y, _color.z, alpha);
}

- (void)buildVectorStrokesIfNeeded {
    if (_vectorStrokes && _vectorWidth == _width && _vectorHeight == _height) return;
    for (CALayer *layer in _vectorStrokes) [layer removeFromSuperlayer];
    _vectorWidth = _width;
    _vectorHeight = _height;
    float waveWidth = fmaxf(1.0f, (float)_width - kWFBleed * 2.0f);

    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _vectorLayer.frame = _metalLayer.frame;

    // Edge fade, as in the shader: the first/last 12 px (or a quarter width).
    float fade = fminf(12.0f, waveWidth * 0.25f);
    float w = (float)_width;
    CAGradientLayer *mask = [CAGradientLayer layer];
    mask.frame = _vectorLayer.bounds;
    mask.startPoint = CGPointMake(0.0, 0.5);
    mask.endPoint = CGPointMake(1.0, 0.5);
    mask.colors = @[(id)NSColor.clearColor.CGColor, (id)NSColor.blackColor.CGColor,
                    (id)NSColor.blackColor.CGColor, (id)NSColor.clearColor.CGColor];
    mask.locations = @[@(kWFBleed / w), @((kWFBleed + fade) / w),
                       @((w - kWFBleed - fade) / w), @((w - kWFBleed) / w)];
    _vectorLayer.mask = mask;

    // Same order and look as the shader: per strand a wide 30% glow under its
    // core, then the highlight's 6 px glow and 2 px core on top.
    NSMutableArray<CAShapeLayer *> *strokes = [NSMutableArray array];
    for (int strand = 0; strand < 5; ++strand) {
        for (int pass = 0; pass < 2; ++pass) {
            BOOL glow = pass == 0;
            CAShapeLayer *shape = [CAShapeLayer layer];
            shape.fillColor = NULL;
            shape.lineCap = kCALineCapRound;
            shape.lineJoin = kCALineJoinRound;
            shape.contentsScale = _backingScale;
            shape.frame = _vectorLayer.bounds;
            float alpha;
            if (strand < 4) {
                shape.lineWidth = kWFThickness[strand] + (glow ? 4.0f : 0.0f);
                alpha = kWFAlphas[strand] * (glow ? 0.3f : 1.0f);
            } else {
                shape.lineWidth = glow ? 6.0f : 2.0f;
                alpha = glow ? 0.4f : 1.0f;
            }
            [shape setValue:@(alpha) forKey:@"wfAlpha"];
            [shape setValue:@(strand) forKey:@"wfStrand"];
            [_vectorLayer addSublayer:shape];
            [strokes addObject:shape];
        }
    }
    _vectorStrokes = strokes;
    [CATransaction commit];
}

- (void)refreshVectorColors {
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    for (CAShapeLayer *shape in _vectorStrokes) {
        CGColorRef stroke = [self createColorWithAlpha:[[shape valueForKey:@"wfAlpha"] floatValue]];
        shape.strokeColor = stroke;
        CGColorRelease(stroke);
    }
    [CATransaction commit];
}

- (void)applyVectorAnimationsFromLoopPosition:(double)loopPos {
    BOOL reduceMotion = NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    float waveWidth = fmaxf(1.0f, (float)_width - kWFBleed * 2.0f);
    float waveHeight = fmaxf(1.0f, (float)_height - kWFBleed * 2.0f);
    int frames = (int)ceil(kWFLoopSeconds * kWFKeyframesPerSecond);

    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    for (int strand = 0; strand < 5; ++strand) {
        CGPathRef still = WFCreateStrandPath(strand, waveWidth, waveHeight,
                                             (float)(loopPos * kWFWaveRate),
                                             (float)(loopPos * kWFBreathRate));
        CAKeyframeAnimation *flow = nil;
        if (!reduceMotion) {
            NSMutableArray *values = [NSMutableArray arrayWithCapacity:(NSUInteger)frames + 1];
            for (int i = 0; i <= frames; ++i) {
                double seconds = kWFLoopSeconds * (double)i / (double)frames;
                CGPathRef path = WFCreateStrandPath(strand, waveWidth, waveHeight,
                                                    (float)(seconds * kWFWaveRate),
                                                    (float)(seconds * kWFBreathRate));
                [values addObject:(__bridge_transfer id)path];
            }
            flow = [CAKeyframeAnimation animationWithKeyPath:@"path"];
            flow.values = values;
            flow.duration = kWFLoopSeconds;
            flow.repeatCount = HUGE_VALF;
            flow.calculationMode = kCAAnimationLinear;
            flow.timeOffset = loopPos;  // start in phase with the last Metal frame
            flow.removedOnCompletion = NO;
        }
        for (CAShapeLayer *shape in _vectorStrokes) {
            if ([[shape valueForKey:@"wfStrand"] intValue] != strand) continue;
            shape.path = still;  // model value; the whole frame under Reduce Motion
            [shape removeAnimationForKey:kWFFlowKey];
            if (flow) [shape addAnimation:flow forKey:kWFFlowKey];
        }
        CGPathRelease(still);
    }
    [CATransaction commit];
}

- (double)currentVectorLoopPosition {
    double elapsed = CACurrentMediaTime() - _vectorStartTime;
    return fmod(_vectorStartLoopPos + elapsed, kWFLoopSeconds);
}

- (void)crossfadeMetal:(float)metalOpacity vector:(float)vectorOpacity {
    [CATransaction begin];
    [CATransaction setAnimationDuration:kWFCrossfadeSeconds];
    [CATransaction setAnimationTimingFunction:
        [CAMediaTimingFunction functionWithName:kCAMediaTimingFunctionEaseInEaseOut]];
    _metalLayer.opacity = metalOpacity;
    _vectorLayer.opacity = vectorOpacity;
    [CATransaction commit];
}

- (void)enterVectorIdle {
    os_unfair_lock_lock(&_lock);
    _settleRequested = NO;
    BOOL quiet = _idle && _audioLevel < 0.01f && _targetAudioLevel < 0.01f;
    // Carry the last Metal frame's phase (timed for its display moment) to now.
    double loopPos = fmod((double)_waveTime / kWFWaveRate
                          + (CACurrentMediaTime() - _lastFrameTime), kWFLoopSeconds);
    if (loopPos < 0.0) loopPos += kWFLoopSeconds;
    os_unfair_lock_unlock(&_lock);
    if (!quiet || _vectorActive || _stopped || _rendererHidden || _width <= 1 || _height <= 1) return;
    _vectorActive = YES;
    _vectorGeneration += 1;
    [self buildVectorStrokesIfNeeded];
    [self refreshVectorColors];
    _vectorStartLoopPos = loopPos;
    _vectorStartTime = CACurrentMediaTime();
    [self applyVectorAnimationsFromLoopPosition:loopPos];
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _vectorLayer.hidden = NO;
    [CATransaction commit];
    [self crossfadeMetal:0.0f vector:1.0f];
    // Metal keeps drawing through the fade so both halves move together; then
    // it stops and the render server alone animates the idle wave.
    NSUInteger generation = _vectorGeneration;
    __weak WFOverlayRenderer *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((kWFCrossfadeSeconds + 0.05) * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        WFOverlayRenderer *strongSelf = weakSelf;
        if (strongSelf && strongSelf.vectorActive && strongSelf.vectorGeneration == generation) {
            [strongSelf stopTimer];
        }
    });
}

- (void)leaveVectorIdle {
    if (!_vectorActive) return;
    _vectorActive = NO;
    _vectorGeneration += 1;
    // Resume Metal exactly where the vector loop is now.
    os_unfair_lock_lock(&_lock);
    _waveTime = (float)([self currentVectorLoopPosition] * kWFWaveRate);
    _lastFrameTime = CACurrentMediaTime();
    os_unfair_lock_unlock(&_lock);
    [_clock start];  // first frame lands on the next refresh
    [self crossfadeMetal:1.0f vector:0.0f];
    NSUInteger generation = _vectorGeneration;
    __weak WFOverlayRenderer *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((kWFCrossfadeSeconds + 0.05) * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        WFOverlayRenderer *strongSelf = weakSelf;
        if (!strongSelf || strongSelf.vectorActive || strongSelf.vectorGeneration != generation) return;
        [CATransaction begin];
        [CATransaction setDisableActions:YES];
        strongSelf.vectorLayer.hidden = YES;
        for (CAShapeLayer *shape in strongSelf.vectorStrokes) [shape removeAnimationForKey:kWFFlowKey];
        [CATransaction commit];
    });
}

- (void)wake {
    os_unfair_lock_lock(&_lock);
    _idleSince = CACurrentMediaTime();
    os_unfair_lock_unlock(&_lock);
    if (_stopped || _rendererHidden) return;
    if (_vectorActive) {
        [self leaveVectorIdle];
    } else {
        [self startTimer];
    }
}

- (void)setIdleState:(BOOL)idle {
    if (idle == _idle) return;
    os_unfair_lock_lock(&_lock);
    _idle = idle;
    os_unfair_lock_unlock(&_lock);
    [self wake];
}

// ---------------- Metal ----------------

// Render thread. ``now`` is this frame's display time (see WFRenderClock).
- (void)renderClockTickAt:(CFTimeInterval)now {
    if (_stopped || _rendererHidden) return;
    os_unfair_lock_lock(&_lock);
    float dt = (float)fmin(fmax(now - _lastFrameTime, 0.0), 0.1);
    _lastFrameTime = now;
    _waveTime += dt * kWFWaveRate;
    // Keep the float small: every term is periodic in the loop.
    float loopUnits = (float)(kWFLoopSeconds * kWFWaveRate);
    if (_waveTime > loopUnits * 64.0f) _waveTime = fmodf(_waveTime, loopUnits);
    float tau = _targetAudioLevel > _audioLevel ? kWFLevelAttack : kWFLevelRelease;
    _audioLevel += (_targetAudioLevel - _audioLevel) * (1.0f - expf(-dt / tau));
    BOOL sized = _width > 1 && _height > 1;
    WFOverlayUniforms uniforms = {
        .dims = (vector_float4){(float)_width, (float)_height, _waveTime, [self breathTime]},
        // Match the Qt path renderer: the wave layout box has 4px of
        // vertical breathing room before the pill clips its outer glow.
        .params = (vector_float4){_audioLevel, (float)_backingScale, kWFBleed, 0.0f},
        .color = _color,
    };
    BOOL settle = !_vectorActive && !_settleRequested && _idle
        && _audioLevel < 0.01f && _targetAudioLevel < 0.01f
        && CACurrentMediaTime() - _idleSince >= kWFVectorAfterSeconds;
    if (settle) _settleRequested = YES;
    os_unfair_lock_unlock(&_lock);
    if (!sized) return;

    id<CAMetalDrawable> drawable = [_metalLayer nextDrawable];
    if (!drawable) return;
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

    if (settle) {
        __weak WFOverlayRenderer *weakSelf = self;
        dispatch_async(dispatch_get_main_queue(), ^{ [weakSelf enterVectorIdle]; });
    }
}

- (void)setFrameX:(double)x y:(double)y width:(int)width height:(int)height {
    BOOL resized = width != _width || height != _height;
    os_unfair_lock_lock(&_lock);
    _width = width;
    _height = height;
    os_unfair_lock_unlock(&_lock);
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.frame = CGRectMake(x, y, width, height);
    _metalLayer.drawableSize = CGSizeMake(
        ceil(width * _backingScale), ceil(height * _backingScale)
    );
    _vectorLayer.frame = _metalLayer.frame;
    [CATransaction commit];
    if (resized) [self wake];  // strokes are rebuilt for the new size next idle
}

- (void)setAudioLevel:(float)audioLevel color:(vector_float4)color {
    float level = fmaxf(0.0f, fminf(audioLevel, 1.0f));
    os_unfair_lock_lock(&_lock);
    _targetAudioLevel = level;  // the render thread eases toward it
    BOOL recolored = !simd_equal(color, _color);
    _color = color;
    os_unfair_lock_unlock(&_lock);
    if (level >= 0.01f || recolored) [self wake];
}

- (void)setRendererHidden:(BOOL)hidden {
    if (hidden == _rendererHidden) return;  // the Qt side re-asserts this every paint
    _rendererHidden = hidden;
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.hidden = hidden;
    if (hidden) {
        _vectorLayer.hidden = YES;
        for (CAShapeLayer *shape in _vectorStrokes) [shape removeAnimationForKey:kWFFlowKey];
        _metalLayer.opacity = 1.0f;
        _vectorLayer.opacity = 0.0f;
    }
    [CATransaction commit];
    if (hidden) {
        _vectorActive = NO;
        _vectorGeneration += 1;
        [self stopTimer];  // nothing is visible: no wakeups at all
    } else {
        os_unfair_lock_lock(&_lock);
        _lastFrameTime = CACurrentMediaTime();
        os_unfair_lock_unlock(&_lock);
        [self wake];
    }
}

- (void)stop {
    _stopped = YES;
    [_clock invalidate];
    [_metalLayer removeFromSuperlayer];
    [_vectorLayer removeFromSuperlayer];
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
