// Compile locally generated Core ML packages using macOS, not Xcode/coremlc.
#import <Foundation/Foundation.h>
#import <CoreML/CoreML.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 2 && strcmp(argv[1], "--help") == 0) {
            puts("Usage: coreml-compile input.mlpackage output.mlmodelc");
            return 0;
        }
        if (argc != 3) return 2;
        NSError *error = nil;
        NSURL *input = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[1]]];
        NSURL *output = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[2]]];
        if ([[NSFileManager defaultManager] fileExistsAtPath:output.path]) return 2;
        NSURL *compiled = [MLModel compileModelAtURL:input error:&error];
        if (!compiled || ![[NSFileManager defaultManager] moveItemAtURL:compiled toURL:output error:&error]) {
            // Do not log paths, weights, or text from model inputs.
            fprintf(stderr, "Core ML local compilation failed (%ld)\n", (long)error.code);
            return 1;
        }
        return 0;
    }
}
