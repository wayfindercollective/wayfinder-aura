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

@interface WFHeroRenderer : NSObject
@property(nonatomic, strong) CAMetalLayer *metalLayer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> commandQueue;
@property(nonatomic, strong) id<MTLComputePipelineState> pipeline;
@property(nonatomic, strong) NSTimer *timer;
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

    _metalLayer = [CAMetalLayer layer];
    _metalLayer.name = @"WayfinderHeroWaveformMetalNative";
    _metalLayer.device = _device;
    _metalLayer.pixelFormat = MTLPixelFormatBGRA8Unorm;
    // The compute kernel writes the drawable, which needs ShaderWrite usage.
    // framebufferOnly=YES gives render-target-only textures; on M3 the
    // invalid write shows up as solid magenta.
    _metalLayer.framebufferOnly = NO;
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

    _lastFrameTime = CACurrentMediaTime();
    _waveTime = 0.0f;
    _morph = 0.0f;
    _targetMorph = 0.0f;
    _audioLevel = 0.0f;
    _targetAudioLevel = 0.0f;
    _strokeScale = 1.0f;
    _color = (vector_float4){91.0f / 255.0f, 143.0f / 255.0f, 212.0f / 255.0f, 1.0f};
    _bg = (vector_float4){30.0f / 255.0f, 30.0f / 255.0f, 31.0f / 255.0f, 1.0f};

    _timer = [NSTimer timerWithTimeInterval:(1.0 / 30.0)
                                     target:self
                                   selector:@selector(drawFrame:)
                                   userInfo:nil
                                    repeats:YES];
    [[NSRunLoop mainRunLoop] addTimer:_timer forMode:NSRunLoopCommonModes];
    return self;
}

- (void)drawFrame:(NSTimer *)timer {
    (void)timer;
    if (_stopped || _metalLayer.hidden || _width <= 1 || _height <= 1) return;
    @autoreleasepool {
        CFTimeInterval now = CACurrentMediaTime();
        float dt = (float)fmin(fmax(now - _lastFrameTime, 0.0), 0.1);
        _lastFrameTime = now;
        _morph += (_targetMorph - _morph) * 0.25f;
        float speed = 2.4f + (9.0f - 2.4f) * _morph;
        _waveTime += dt * speed;
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
    }
}

- (void)setFrameX:(double)x y:(double)y width:(int)width height:(int)height {
    _width = width;
    _height = height;
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.frame = CGRectMake(x, y, width, height);
    _metalLayer.drawableSize = CGSizeMake(width, height);
    [CATransaction commit];
}

- (void)setActive:(BOOL)active
        audioLevel:(float)audioLevel
             color:(vector_float4)color
                bg:(vector_float4)bg
       strokeScale:(float)strokeScale {
    _targetMorph = active ? 1.0f : 0.0f;
    _targetAudioLevel = active ? fmaxf(0.0f, fminf(audioLevel, 1.0f)) : 0.0f;
    _color = color;
    _bg = bg;
    _strokeScale = fmaxf(0.7f, fminf(strokeScale, 2.5f));
}

- (void)setRendererHidden:(BOOL)hidden {
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    _metalLayer.hidden = hidden;
    [CATransaction commit];
    _lastFrameTime = CACurrentMediaTime();
}

- (void)stop {
    _stopped = YES;
    [_timer invalidate];
    _timer = nil;
    [_metalLayer removeFromSuperlayer];
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
