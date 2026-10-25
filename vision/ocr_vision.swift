import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count >= 2 else {
    fputs("usage: ocr_vision <image>\n", stderr)
    exit(2)
}

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("cannot load image: \(path)\n", stderr)
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
if CommandLine.arguments.count >= 3 {
    request.recognitionLanguages = CommandLine.arguments[2]
        .split(separator: ",")
        .map { String($0) }
} else {
    request.recognitionLanguages = ["zh-Hans", "en-US"]
}
request.usesLanguageCorrection = false

let imageWidth = CGFloat(cgImage.width)
let imageHeight = CGFloat(cgImage.height)

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
    guard let observations = request.results else {
        exit(0)
    }
    for observation in observations {
        if let candidate = observation.topCandidates(1).first {
            let box = observation.boundingBox
            let x = box.origin.x * imageWidth
            let y = (1.0 - box.origin.y - box.size.height) * imageHeight
            let w = box.size.width * imageWidth
            let h = box.size.height * imageHeight
            let text = candidate.string
                .replacingOccurrences(of: "\t", with: " ")
                .replacingOccurrences(of: "\n", with: " ")
            print("\(x)\t\(y)\t\(w)\t\(h)\t\(text)")
        }
    }
} catch {
    fputs("ocr failed: \(error)\n", stderr)
    exit(4)
}
