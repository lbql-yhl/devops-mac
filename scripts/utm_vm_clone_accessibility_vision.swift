#!/usr/bin/swift
import AppKit
import Foundation
import Vision

struct OCRItem: Codable {
    let text: String
    let confidence: Float
    let x: CGFloat
    let y: CGFloat
    let width: CGFloat
    let height: CGFloat
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: utm_vm_clone_accessibility_vision.swift <image.png>\n".utf8))
    exit(2)
}

let imagePath = CommandLine.arguments[1]
guard
    let image = NSImage(contentsOfFile: imagePath),
    let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
else {
    FileHandle.standardError.write(Data("image unavailable\n".utf8))
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US", "zh-Hans"]

let handler = VNImageRequestHandler(cgImage: cgImage, orientation: .up, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write(Data("vision request failed\n".utf8))
    exit(4)
}

let items: [OCRItem] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let box = observation.boundingBox
    return OCRItem(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.origin.x,
        y: box.origin.y,
        width: box.size.width,
        height: box.size.height
    )
}

do {
    let data = try JSONEncoder().encode(items)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    FileHandle.standardError.write(Data("json encoding failed\n".utf8))
    exit(5)
}
