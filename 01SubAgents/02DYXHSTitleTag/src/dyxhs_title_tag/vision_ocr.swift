// macOS built-in Vision OCR. No model download or third-party dependency.
import Foundation
import Vision
import ImageIO

for path in CommandLine.arguments.dropFirst() {
    var record: [String: Any] = ["path": path, "engine": "apple-vision"]
    do {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        request.usesLanguageCorrection = true
        request.minimumTextHeight = 0.008
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: path), options: [:])
        try handler.perform([request])
        let observations = (request.results ?? []).sorted {
            if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.02 {
                return $0.boundingBox.midY > $1.boundingBox.midY
            }
            return $0.boundingBox.minX < $1.boundingBox.minX
        }
        record["lines"] = observations.compactMap { observation -> [String: Any]? in
            guard let text = observation.topCandidates(1).first else { return nil }
            let box = observation.boundingBox
            return ["text": text.string, "confidence": text.confidence,
                    "box": ["x": box.minX, "y": box.minY,
                            "width": box.width, "height": box.height]]
        }
    } catch {
        record["error"] = String(describing: error)
        record["lines"] = []
    }
    let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)
}
