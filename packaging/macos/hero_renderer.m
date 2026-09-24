#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#import <simd/simd.h>

typedef struct {
    vector_float4 dims;
    vector_float4 params;
    vector_float4 color;
    vector_float4 bg;
} WFHeroUniforms;

// ---------------------------------------------------------------------------
// The calm (idle) ribbon runs on Core Animation, not Metal.
//
// A continuously presenting Metal client holds ~256 MB of GPU memory and 30
// wakeups/s (M3 Ultra, macOS 27). Once the ribbon is calm, its strands are
// handed to vector CAShapeLayers whose paths the render server animates: no
// per-frame work in Aura, no Metal memory, native-resolution strokes and the
// display's refresh rate. Recording (and any morph back) returns to Metal.
//
// The vector strands evaluate the calm branch of the Metal shader
// (src/wayfinder/ui/macos_hero_metal.py, morph 0). The time rates make every
// term finish whole cycles in kWFHeroLoopUnits, so the keyframe loop is
// seamless and both hand-offs keep phase.
// ---------------------------------------------------------------------------
static const float kWFHeroCalmSpeed = 2.4f;              // wave units per second at morph 0
static const double kWFHeroLoopUnits = 10.0 * M_PI;      // 5/8/10 + 7/10 cycles, 4 breaths
static const int kWFHeroKeyframesPerSecond = 20;
static const CFTimeInterval kWFHeroVectorAfterSeconds = 1.0;
static const CFTimeInterval kWFHeroCrossfadeSeconds = 0.35;
static const int kWFHeroPointCount = 109;                // the shader's calm point count
static NSString *const kWFHeroFlowKey = @"wfHeroFlow";

static const float kWFHeroFreqs[4] = {0.07f, 0.11f, 0.16f, 0.22f};
static const float kWFHeroPhases[4] = {0.0f, 1.0f, 2.2f, 0.7f};
static const float kWFHeroAlphas[4] = {0.15f, 0.25f, 0.40f, 0.55f};
static const float kWFHeroThickness[4] = {6.0f, 5.0f, 4.0f, 3.0f};

static float WFHeroSoftLimit(float dy, float height, float strokeScale) {
    float maxStroke = roundf(6.0f * strokeScale);
    float glowExtra = roundf(2.0f * strokeScale);
    float maxStrokeRadius = (maxStroke + glowExtra * 2.0f) * 0.5f;
    float aMax = fmaxf(4.0f, height * 0.5f - maxStrokeRadius - 3.0f);
    float knee = aMax * 0.7f;
    float softRange = aMax - knee;
    float magnitude = fabsf(dy);
    if (magnitude <= knee) return dy;
    return copysignf(knee + softRange * tanhf((magnitude - knee) / softRange), dy);
}

// Strand 0-3: the waves; 4: the highlight. Calm branch (morph 0, no level).
static CGPathRef WFHeroCreateStrandPath(int strand, float width, float height, float t, float strokeScale) {
    float maxAmp = height * 0.42f;
    float breath = 0.26f + 0.09f * (0.5f + 0.5f * sinf(t * 0.8f));
    float amp = maxAmp * fminf(0.80f, breath);
    CGMutablePathRef path = CGPathCreateMutable();
    float step = width / (float)(kWFHeroPointCount - 1);
    for (int i = 0; i < kWFHeroPointCount; ++i) {
        float x = step * (float)i;
        float u = 640.0f * x / fmaxf(width - 1.0f, 1.0f);
        float dy;
        if (strand < 4) {
            float f = kWFHeroFreqs[strand] * 0.32f;
            float phase = kWFHeroPhases[strand];
            dy = amp * sinf(f * u + t + phase);
            dy += amp * 0.4f * sinf(f * 2.3f * u + t * 1.6f + phase);
            dy += amp * 0.2f * sinf(f * 3.7f * u + t * 2.0f + phase * 0.5f);
        } else {
            dy = amp * sinf(0.13f * 0.32f * u + t * 1.4f);
            dy += amp * 0.5f * sinf(0.26f * 0.32f * u + t * 2.0f + 0.8f);
        }
        float y = fminf(fmaxf(height * 0.5f + WFHeroSoftLimit(dy, height, strokeScale), 1.0f), height - 1.0f);
        if (i == 0) CGPathMoveToPoint(path, NULL, x, y);
        else CGPathAddLineToPoint(path, NULL, x, y);
    }
    return path;
}

@interface WFHeroRenderer : NSObject
@property(nonatomic, strong) CAMetalLayer *metalLayer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> commandQueue;
@property(nonatomic, strong) id<MTLComputePipelineState> pipeline;
@property(nonatomic, strong) NSTimer *timer;
@property(nonatomic) CGFloat backingScale;
@property(nonatomic) CFTimeInterval lastFrameTime;
@property(nonatomic) float waveTime;
@property(nonatomic) float morph;
@property(nonatomic) float targetMorph;
@property(nonatomic) float audioLevel;
@property(nonatomic) float targetAudioLevel;
@property(nonatomic) float strokeScale;
@property(nonatomic) vector_float4 color;
@property(nonatomic) vector_float4 bg;
@property(nonatomic) int width;
@property(nonatomic) int height;
@property(nonatomic) BOOL stopped;
@property(nonatomic) BOOL rendererHidden;
@property(nonatomic) BOOL windowVisible;
@property(nonatomic) CFTimeInterval idleSince;
@property(nonatomic, strong) NSMutableArray *observers;
// Vector calm ribbon
@property(nonatomic, strong) CALayer *vectorLayer;      // opaque card colour
@property(nonatomic, strong) CALayer *vectorStrands;    // edge-faded strokes
@property(nonatomic, strong) NSArray<CAShapeLayer *> *vectorStrokes;
@property(nonatomic) int vectorWidth;
@property(nonatomic) int vectorHeight;
@property(nonatomic) float vectorStrokeScale;
@property(nonatomic) BOOL vectorActive;
@property(nonatomic) CFTimeInterval vectorStartTime;
@property(nonatomic) double vectorStartLoopUnits;
@property(nonatomic) NSUInteger vectorGeneration;
@end

@implementation WFHeroRenderer

- (instancetype)initWithParentLayer:(CALayer *)parentLayer
                        backingScale:(CGFloat)backingScale
                        shaderSource:(NSString *)shaderSource {
    self = [super init];
    if (!self) return nil;

    _device = MTLCreateSystemDefaultDevice();
    if (!_device) return nil;
    NSError *error = nil;
    id<MTLLibrary> library = [_device newLibraryWithSource:shaderSource
                                                  options:nil
                                                    error:&error];
    if (!library) {
        NSLog(@"Wayfinder hero Metal compile failed: %@", error);
        return nil;
    }
    id<MTLFunction> function = [library newFunctionWithName:@"hero_wave"];
    _pipeline = [_device newComputePipelineStateWithFunction:function error:&error];
    if (!_pipeline) {
        NSLog(@"Wayfinder hero Metal pipeline failed: %@", error);
        return nil;
    }
    _commandQueue = [_device newCommandQueue];
    if (!_commandQueue) return nil;

    _backingScale = fmax(1.0, backingScale);
    _metalLayer = [CAMetalLayer layer];
    _metalLayer.name = @"WayfinderHeroWaveformMetalNative";
    _metalLayer.device = _device;
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
    _metalLayer.opaque = YES;
    _metalLayer.masksToBounds = YES;
    _metalLayer.contentsGravity = kCAGravityResize;
    _metalLayer.magnificationFilter = kCAFilterLinear;
    _metalLayer.minificationFilter = kCAFilterLinear;
    _metalLayer.contentsScale = backingScale;
    _metalLayer.zPosition = 1000.0;
    [parentLayer addSublayer:_metalLayer];

    _vectorLayer = [CALayer layer];
    _vectorLayer.name = @"WayfinderHeroWaveformVector";
    _vectorLayer.geometryFlipped = YES;  // y grows downward, as in the shader
    _vectorLayer.masksToBounds = YES;
    _vectorLayer.contentsScale = _backingScale;
    _vectorLayer.zPosition = 1001.0;
    _vectorLayer.opacity = 0.0f;
    _vectorLayer.hidden = YES;
    _vectorStrands = [CALayer layer];
    _vectorStrands.contentsScale = _backingScale;
    [_vectorLayer addSublayer:_vectorStrands];
    [parentLayer addSublayer:_vectorLayer];

    _lastFrameTime = CACurrentMediaTime();
    _waveTime = 0.0f;
    _morph = 0.0f;
    _targetMorph = 0.0f;
    _audioLevel = 0.0f;
    _targetAudioLevel = 0.0f;
    _strokeScale = 1.0f;
    _color = (vector_float4){91.0f / 255.0f, 143.0f / 255.0f, 212.0f / 255.0f, 1.0f};
    _bg = (vector_float4){30.0f / 255.0f, 30.0f / 255.0f, 31.0f / 255.0f, 1.0f};

    _windowVisible = YES;
    _idleSince = CACurrentMediaTime();
    _observers = [NSMutableArray array];
    __weak WFHeroRenderer *weakSelf = self;
    [_observers addObject:[NSNotificationCenter.defaultCenter
        addObserverForName:NSWindowDidChangeOcclusionStateNotification
                    object:nil queue:NSOperationQueue.mainQueue
                usingBlock:^(NSNotification *note) {
        [weakSelf windowOcclusionChanged:note.object];
    }]];
    [self startTimer];
    return self;
}

- (void)startTimer {
    if (_timer || _stopped) return;
    _lastFrameTime = CACurrentMediaTime();
    _timer = [NSTimer timerWithTimeInterval:(1.0 / 30.0)
                                     target:self
                                   selector:@selector(drawFrame:)
                                   userInfo:nil
                                    repeats:YES];
    [[NSRunLoop mainRunLoop] addTimer:_timer forMode:NSRunLoopCommonModes];
}

- (void)stopTimer {
    [_timer invalidate];
    _timer = nil;
}

- (BOOL)ownsWindow:(NSWindow *)window {
    CALayer *root = window.contentView.layer;
    for (CALayer *layer = _metalLayer; layer != nil; layer = layer.superlayer) {
        if (layer == root) return YES;
    }
    return NO;
}

- (void)windowOcclusionChanged:(NSWindow *)window {
    if (![window isKindOfClass:NSWindow.class] || ![self ownsWindow:window]) return;
    _windowVisible = (window.occlusionState & NSWindowOcclusionStateVisible) != 0;
    if (_windowVisible) {
        [self wake];
    } else if (!_vectorActive) {
        [self stopTimer];  // nobody can see it: no Metal work at all
    }
}

- (BOOL)calm {
    return _targetMorph == 0.0f && _morph < 0.01f && _targetAudioLevel <= 0.0f;
}

// ---------------- vector calm ribbon ----------------

- (CGColorRef)createStrokeColorWithAlpha:(float)alpha {
    return CGColorCreateSRGB(_color.x, _color.y, _color.z, alpha);
}

- (void)buildVectorStrokesIfNeeded {
    if (_vectorStrokes && _vectorWidth == _width && _vectorHeight == _height
            && _vectorStrokeScale == _strokeScale) return;
    for (CALayer *layer in _vectorStrokes) [layer removeFromSuperlayer];
    _vectorWidth = _width;
    _vectorHeight = _height;
    _vectorStrokeScale = _strokeScale;
    float w = (float)_width;
    float s = _strokeScale;

    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _vectorLayer.frame = _metalLayer.frame;
    _vectorStrands.frame = _vectorLayer.bounds;

    // Edge fade on the strokes only; the card colour below stays opaque.
    float fade = fmaxf(24.0f, w * 0.06f);
    CAGradientLayer *mask = [CAGradientLayer layer];
    mask.frame = _vectorStrands.bounds;
    mask.startPoint = CGPointMake(0.0, 0.5);
    mask.endPoint = CGPointMake(1.0, 0.5);
    mask.colors = @[(id)NSColor.clearColor.CGColor, (id)NSColor.blackColor.CGColor,
                    (id)NSColor.blackColor.CGColor, (id)NSColor.clearColor.CGColor];
    mask.locations = @[@0.0, @(fade / w), @(1.0 - fade / w), @1.0];
    _vectorStrands.mask = mask;

    float glowExtra = fmaxf(1.0f, roundf(2.0f * s));
    float blur = fmaxf(1.0f, 2.0f * s);
    const float brightness = 0.55f;    // calm
    const float hiBrightness = 0.40f;  // calm

    // Shader order: every glow first (soft wide strokes stand in for its
    // Gaussian falloff), then the cores dim-to-bright, then the highlight.
    NSMutableArray<CAShapeLayer *> *strokes = [NSMutableArray array];
    void (^add)(int, float, float) = ^(int strand, float lineWidth, float alpha) {
        CAShapeLayer *shape = [CAShapeLayer layer];
        shape.fillColor = NULL;
        shape.lineCap = kCALineCapRound;
        shape.lineJoin = kCALineJoinRound;
        shape.contentsScale = self.backingScale;
        shape.frame = self.vectorStrands.bounds;
        shape.lineWidth = lineWidth;
        [shape setValue:@(alpha) forKey:@"wfAlpha"];
        [shape setValue:@(strand) forKey:@"wfStrand"];
        [self.vectorStrands addSublayer:shape];
        [strokes addObject:shape];
    };
    for (int i = 0; i < 4; ++i) {
        float core = fmaxf(1.0f, roundf(kWFHeroThickness[i] * s));
        float glowWidth = core + glowExtra * 2.0f;
        float glowAlpha = kWFHeroAlphas[i] * 0.3f * brightness;
        add(i, glowWidth + blur * 3.0f, glowAlpha * 0.35f);
        add(i, glowWidth, glowAlpha);
    }
    float hiGlow = fmaxf(2.0f, roundf(4.0f * s));
    add(4, hiGlow + blur * 3.0f, 0.4f * hiBrightness * 0.35f);
    add(4, hiGlow, 0.4f * hiBrightness);
    for (int i = 0; i < 4; ++i) {
        add(i, fmaxf(1.0f, roundf(kWFHeroThickness[i] * s)), kWFHeroAlphas[i] * brightness);
    }
    add(4, fmaxf(1.0f, roundf(2.0f * s)), 0.95f * hiBrightness);
    _vectorStrokes = strokes;
    [CATransaction commit];
}

- (void)refreshVectorColors {
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    CGColorRef card = CGColorCreateSRGB(_bg.x, _bg.y, _bg.z, 1.0);
    _vectorLayer.backgroundColor = card;
    CGColorRelease(card);
    for (CAShapeLayer *shape in _vectorStrokes) {
        CGColorRef stroke = [self createStrokeColorWithAlpha:[[shape valueForKey:@"wfAlpha"] floatValue]];
        shape.strokeColor = stroke;
        CGColorRelease(stroke);
    }
    [CATransaction commit];
}

- (void)applyVectorAnimationsFromLoopUnits:(double)loopUnits {
    BOOL reduceMotion = NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    float w = (float)_width;
    float h = (float)_height;
    double loopSeconds = kWFHeroLoopUnits / kWFHeroCalmSpeed;
    int frames = (int)ceil(loopSeconds * kWFHeroKeyframesPerSecond);

    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    for (int strand = 0; strand < 5; ++strand) {
        CGPathRef still = WFHeroCreateStrandPath(strand, w, h, (float)loopUnits, _strokeScale);
        CAKeyframeAnimation *flow = nil;
        if (!reduceMotion) {
            NSMutableArray *values = [NSMutableArray arrayWithCapacity:(NSUInteger)frames + 1];
            for (int i = 0; i <= frames; ++i) {
                float t = (float)(kWFHeroLoopUnits * (double)i / (double)frames);
                [values addObject:(__bridge_transfer id)WFHeroCreateStrandPath(strand, w, h, t, _strokeScale)];
            }
            flow = [CAKeyframeAnimation animationWithKeyPath:@"path"];
            flow.values = values;
            flow.duration = loopSeconds;
            flow.repeatCount = HUGE_VALF;
            flow.calculationMode = kCAAnimationLinear;
            flow.timeOffset = loopUnits / kWFHeroCalmSpeed;  // in phase with Metal
            flow.removedOnCompletion = NO;
        }
        for (CAShapeLayer *shape in _vectorStrokes) {
            if ([[shape valueForKey:@"wfStrand"] intValue] != strand) continue;
            shape.path = still;
            [shape removeAnimationForKey:kWFHeroFlowKey];
            if (flow) [shape addAnimation:flow forKey:kWFHeroFlowKey];
        }
        CGPathRelease(still);
    }
    [CATransaction commit];
}

- (double)currentVectorLoopUnits {
    double elapsed = CACurrentMediaTime() - _vectorStartTime;
    return fmod(_vectorStartLoopUnits + elapsed * kWFHeroCalmSpeed, kWFHeroLoopUnits);
}

- (void)crossfadeMetal:(float)metalOpacity vector:(float)vectorOpacity {
    [CATransaction begin];
    [CATransaction setAnimationDuration:kWFHeroCrossfadeSeconds];
    [CATransaction setAnimationTimingFunction:
        [CAMediaTimingFunction functionWithName:kCAMediaTimingFunctionEaseInEaseOut]];
    _metalLayer.opacity = metalOpacity;
    _vectorLayer.opacity = vectorOpacity;
    [CATransaction commit];
}

- (void)enterVectorIdle {
    if (_vectorActive || _stopped || _rendererHidden || _width <= 1 || _height <= 1) return;
    _vectorActive = YES;
    _vectorGeneration += 1;
    [self buildVectorStrokesIfNeeded];
    [self refreshVectorColors];
    double loopUnits = fmod((double)_waveTime, kWFHeroLoopUnits);
    _vectorStartLoopUnits = loopUnits;
    _vectorStartTime = CACurrentMediaTime();
    [self applyVectorAnimationsFromLoopUnits:loopUnits];
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _vectorLayer.hidden = NO;
    [CATransaction commit];
    [self crossfadeMetal:0.0f vector:1.0f];
    NSUInteger generation = _vectorGeneration;
    __weak WFHeroRenderer *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((kWFHeroCrossfadeSeconds + 0.05) * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        WFHeroRenderer *strongSelf = weakSelf;
        if (strongSelf && strongSelf.vectorActive && strongSelf.vectorGeneration == generation) {
            [strongSelf stopTimer];
        }
    });
}

- (void)leaveVectorIdle {
    if (!_vectorActive) return;
    _vectorActive = NO;
    _vectorGeneration += 1;
    _waveTime = (float)[self currentVectorLoopUnits];  // Metal resumes in phase
    [self startTimer];
    [self drawFrame:nil];
    [self crossfadeMetal:1.0f vector:0.0f];
    NSUInteger generation = _vectorGeneration;
    __weak WFHeroRenderer *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((kWFHeroCrossfadeSeconds + 0.05) * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        WFHeroRenderer *strongSelf = weakSelf;
        if (!strongSelf || strongSelf.vectorActive || strongSelf.vectorGeneration != generation) return;
        [CATransaction begin];
        [CATransaction setDisableActions:YES];
        strongSelf.vectorLayer.hidden = YES;
        for (CAShapeLayer *shape in strongSelf.vectorStrokes) [shape removeAnimationForKey:kWFHeroFlowKey];
        [CATransaction commit];
    });
}

- (void)wake {
    _idleSince = CACurrentMediaTime();
    if (_stopped || _rendererHidden || !_windowVisible) return;
    if (_vectorActive) {
        if ([self calm]) return;  // calm and already flowing on Core Animation
        [self leaveVectorIdle];
    } else {
        [self startTimer];
    }
}

// ---------------- Metal ----------------

- (void)drawFrame:(NSTimer *)timer {
    (void)timer;
    if (_stopped || _rendererHidden || _width <= 1 || _height <= 1) return;
    @autoreleasepool {
        CFTimeInterval now = CACurrentMediaTime();
        float dt = (float)fmin(fmax(now - _lastFrameTime, 0.0), 0.1);
        _lastFrameTime = now;
        _morph += (_targetMorph - _morph) * 0.25f;
        float speed = kWFHeroCalmSpeed + (9.0f - kWFHeroCalmSpeed) * _morph;
        _waveTime += dt * speed;
        if (_waveTime > (float)(kWFHeroLoopUnits * 64.0)) {
            _waveTime = fmodf(_waveTime, (float)kWFHeroLoopUnits);  // every term is periodic
        }
        float smoothFactor = powf(0.7f, 4.0f);
        _audioLevel = _audioLevel * smoothFactor + _targetAudioLevel * (1.0f - smoothFactor);

        id<CAMetalDrawable> drawable = [_metalLayer nextDrawable];
        if (!drawable) return;
        WFHeroUniforms uniforms = {
            .dims = (vector_float4){(float)_width, (float)_height, _waveTime, _audioLevel},
            .params = (vector_float4){_morph, _strokeScale, 0.0f, 0.0f},
            .color = _color,
            .bg = _bg,
        };
        id<MTLCommandBuffer> commandBuffer = [_commandQueue commandBufferWithUnretainedReferences];
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:_pipeline];
        [encoder setTexture:drawable.texture atIndex:0];
        [encoder setBytes:&uniforms length:sizeof(uniforms) atIndex:0];
        [encoder dispatchThreads:MTLSizeMake((NSUInteger)_width, (NSUInteger)_height, 1)
            threadsPerThreadgroup:MTLSizeMake(16, 8, 1)];
        [encoder endEncoding];
        [commandBuffer presentDrawable:drawable];
        [commandBuffer commit];

        if (!_vectorActive && [self calm] && now - _idleSince >= kWFHeroVectorAfterSeconds) {
            [self enterVectorIdle];
        }
    }
}

- (void)setFrameX:(double)x y:(double)y width:(int)width height:(int)height {
    BOOL resized = width != _width || height != _height;
    _width = width;
    _height = height;
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.frame = CGRectMake(x, y, width, height);
    _metalLayer.drawableSize = CGSizeMake(width, height);
    _vectorLayer.frame = _metalLayer.frame;
    [CATransaction commit];
    if (resized) {
        if (_vectorActive) {
            // Rebuild the strands for the new size and keep flowing in phase.
            double units = [self currentVectorLoopUnits];
            [self buildVectorStrokesIfNeeded];
            [self refreshVectorColors];
            _vectorStartLoopUnits = units;
            _vectorStartTime = CACurrentMediaTime();
            [self applyVectorAnimationsFromLoopUnits:units];
        } else {
            [self wake];
        }
    }
}

- (void)setActive:(BOOL)active
        audioLevel:(float)audioLevel
             color:(vector_float4)color
                bg:(vector_float4)bg
       strokeScale:(float)strokeScale {
    float target = active ? 1.0f : 0.0f;
    BOOL changedTarget = target != _targetMorph;
    BOOL recolored = !simd_equal(color, _color) || !simd_equal(bg, _bg);
    float clampedScale = fmaxf(0.7f, fminf(strokeScale, 2.5f));
    BOOL rescaled = clampedScale != _strokeScale;
    _targetMorph = target;
    _targetAudioLevel = active ? fmaxf(0.0f, fminf(audioLevel, 1.0f)) : 0.0f;
    _color = color;
    _bg = bg;
    _strokeScale = clampedScale;
    if (active || changedTarget) {
        [self wake];
    } else if (_vectorActive && rescaled) {
        double units = [self currentVectorLoopUnits];
        [self buildVectorStrokesIfNeeded];
        [self refreshVectorColors];
        _vectorStartLoopUnits = units;
        _vectorStartTime = CACurrentMediaTime();
        [self applyVectorAnimationsFromLoopUnits:units];
    } else if (_vectorActive && recolored) {
        [self refreshVectorColors];
    }
}

- (void)setRendererHidden:(BOOL)hidden {
    if (hidden == _rendererHidden) return;
    _rendererHidden = hidden;
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.hidden = hidden;
    if (hidden) {
        _vectorLayer.hidden = YES;
        for (CAShapeLayer *shape in _vectorStrokes) [shape removeAnimationForKey:kWFHeroFlowKey];
        _metalLayer.opacity = 1.0f;
        _vectorLayer.opacity = 0.0f;
    }
    [CATransaction commit];
    _lastFrameTime = CACurrentMediaTime();
    if (hidden) {
        _vectorActive = NO;
        _vectorGeneration += 1;
        [self stopTimer];  // a hidden layer used to keep 30 empty wakeups/s
    } else {
        [self wake];
    }
}

- (void)stop {
    _stopped = YES;
    [self stopTimer];
    for (id observer in _observers) {
        [NSNotificationCenter.defaultCenter removeObserver:observer];
    }
    [_observers removeAllObjects];
    [_metalLayer removeFromSuperlayer];
    [_vectorLayer removeFromSuperlayer];
}

@end

void *wf_hero_create(void *parentLayerPtr, double backingScale, const char *shaderUTF8) {
    if (!parentLayerPtr || !shaderUTF8) return NULL;
    CALayer *parentLayer = (__bridge CALayer *)parentLayerPtr;
    NSString *shader = [NSString stringWithUTF8String:shaderUTF8];
    WFHeroRenderer *renderer = [[WFHeroRenderer alloc] initWithParentLayer:parentLayer
                                                             backingScale:backingScale
                                                             shaderSource:shader];
    return renderer ? (__bridge_retained void *)renderer : NULL;
}

void wf_hero_set_frame(void *handle, double x, double y, int width, int height) {
    if (!handle) return;
    WFHeroRenderer *renderer = (__bridge WFHeroRenderer *)handle;
    [renderer setFrameX:x y:y width:width height:height];
}

void wf_hero_set_state(void *handle, int active, float audioLevel,
                       float red, float green, float blue,
                       float bgRed, float bgGreen, float bgBlue,
                       float strokeScale) {
    if (!handle) return;
    WFHeroRenderer *renderer = (__bridge WFHeroRenderer *)handle;
    [renderer setActive:(active != 0)
             audioLevel:audioLevel
                  color:(vector_float4){red, green, blue, 1.0f}
                     bg:(vector_float4){bgRed, bgGreen, bgBlue, 1.0f}
            strokeScale:strokeScale];
}

void wf_hero_set_hidden(void *handle, int hidden) {
    if (!handle) return;
    WFHeroRenderer *renderer = (__bridge WFHeroRenderer *)handle;
    [renderer setRendererHidden:(hidden != 0)];
}

void wf_hero_destroy(void *handle) {
    if (!handle) return;
    WFHeroRenderer *renderer = (__bridge_transfer WFHeroRenderer *)handle;
    [renderer stop];
}
