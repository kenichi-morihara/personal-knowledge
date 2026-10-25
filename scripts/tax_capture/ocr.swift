import Foundation
import Vision
import ImageIO

// Native, on-device recognition. No network requests or model API keys.
guard CommandLine.arguments.count == 2 else { exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
      let width = props[kCGImagePropertyPixelWidth] as? Int,
      let height = props[kCGImagePropertyPixelHeight] as? Int,
      width > 0, height > 0, width <= 12000, height <= 12000,
      width * height <= 40000000 else {
    fputs("Invalid image or image exceeds 40 megapixels\n", stderr); exit(2)
}
let orientation = CGImagePropertyOrientation(rawValue: (props[kCGImagePropertyOrientation] as? UInt32) ?? 1) ?? .up
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["ja-JP", "en-US"]
request.usesLanguageCorrection = true
do {
    try VNImageRequestHandler(url: url, orientation: orientation).perform([request])
    let observations = (request.results ?? []).sorted {
        if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.015 {
            return $0.boundingBox.midY > $1.boundingBox.midY
        }
        return $0.boundingBox.minX < $1.boundingBox.minX
    }
    let lines: [[String: Any]] = observations.compactMap { observation in
        guard let value = observation.topCandidates(1).first else { return nil }
        return ["text": value.string, "confidence": value.confidence]
    }
    let data = try JSONSerialization.data(withJSONObject: lines, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    fputs("Image recognition failed\n", stderr); exit(1)
}
