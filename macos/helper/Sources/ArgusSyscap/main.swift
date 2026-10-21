// argus-syscap: system audio for Argus on macOS (ScreenCaptureKit).
//
// CONTRACT WITH THE APP (it is read by src/argus/mac/syscap.py; change only together with
// it)
//
//   stdout   raw audio: int16, little-endian, channels interleaved, sample rate and channel
//            count from the arguments (default 48000 Hz, 2 channels). Nothing but audio.
//   stderr   JSON lines are events: {"event":"ready"}, {"event":"error","code":"…","message":"…"},
//            {"event":"warning","message":"…"}, {"event":"stopped"}. After "error" the helper
//            exits: an error is the end, not a remark; remarks go as a separate "warning" event.
//   stdin    closing it means "the owner died, exit". That way the helper does not stay alive
//            if the app crashes.
//
//   Arguments:
//     --rate N            sample rate, Hz (8000, 16000, 24000, 48000)
//     --channels N        1 or 2
//     --pid N             take audio only from this program (can be repeated)
//     --exclude-pid N     do not take audio from this program (can be repeated); cannot be
//                         combined with --pid
//     --probe             only check whether the permission exists and the system responds;
//                         do not write audio
//
//   Exit codes: 0 normal, 1 failure, 2 no permission to record screen and audio,
//               3 system too old, 4 no display.
//
// Why a separate program rather than a call from Python: assembling audio from a
// CMSampleBuffer is pointers and structures, where a mistake does not crash but corrupts
// the audio. Here the compiler checks the types.
//
// The screen recording permission is granted to the Argus app, not to this program: it
// runs as the app's child process, and the system attributes the request to the
// "responsible" process, Argus.app.

import AVFoundation
import CoreAudio
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - Exit and events

enum ExitCode: Int32 {
    case ok = 0
    case failed = 1
    case permission = 2
    case unsupported = 3
    case noDisplay = 4
}

func emit(_ event: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: event, options: []) else { return }
    var line = data
    line.append(0x0A)
    try? FileHandle.standardError.write(contentsOf: line)
}

func finish(_ code: ExitCode, event: [String: Any]? = nil) -> Never {
    if let event = event {
        emit(event)
    }
    exit(code.rawValue)
}

// MARK: - Arguments

struct Options {
    var rate = 48000
    var channels = 2
    var pids: [Int32] = []
    var excludePids: [Int32] = []
    var probe = false
}

func parseOptions() -> Options {
    var options = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    while !args.isEmpty {
        let flag = args.removeFirst()
        switch flag {
        case "--rate":
            if let value = args.first, let number = Int(value) { options.rate = number; args.removeFirst() }
        case "--channels":
            if let value = args.first, let number = Int(value) { options.channels = number; args.removeFirst() }
        case "--pid":
            if let value = args.first, let number = Int32(value) { options.pids.append(number); args.removeFirst() }
        case "--exclude-pid":
            if let value = args.first, let number = Int32(value) { options.excludePids.append(number); args.removeFirst() }
        case "--probe":
            options.probe = true
        default:
            break
        }
    }
    options.channels = max(1, min(2, options.channels))
    return options
}

// MARK: - Errors

enum CaptureError: Error {
    case noDisplay
    case noApplications
    case unexpectedFormat
}

/// Reduce a system error to a code the app understands.
func classify(_ error: Error) -> (code: String, exit: ExitCode) {
    if let capture = error as? CaptureError {
        switch capture {
        case .noDisplay: return ("no_display", .noDisplay)
        case .noApplications: return ("no_applications", .failed)
        case .unexpectedFormat: return ("format", .failed)
        }
    }
    let nsError = error as NSError
    // -3801 (userDeclined): the person refused, or has not yet allowed screen and audio
    // recording.
    // -3803 (missingEntitlements) does not belong here: it is about the build's signature, not
    // the person's choice, and the advice "allow it in the settings" would not help.
    if nsError.code == -3801 {
        return ("permission", .permission)
    }
    // If the system answered with something else and the screen recording permission is
    // absent, the cause is exactly that.
    if !CGPreflightScreenCaptureAccess() {
        return ("permission", .permission)
    }
    // -3814 (noDisplayList): there is nothing to capture: there is no display at all.
    if nsError.code == -3814 {
        return ("no_display", .noDisplay)
    }
    return ("failed", .failed)
}

// MARK: - Capture

final class SystemAudio: NSObject, SCStreamOutput, SCStreamDelegate {
    private let options: Options
    private var activeStream: SCStream?
    private let streamLock = NSLock()   // the stream is set from the start task and cleared from another thread
    private let queue = DispatchQueue(label: "argus.syscap.audio", qos: .userInteractive)
    private let standardOutput = FileHandle.standardOutput
    private var reportedSkip = false    // touched only by `queue`

    init(options: Options) {
        self.options = options
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw CaptureError.noDisplay
        }

        let filter: SCContentFilter
        if options.pids.isEmpty {
            // The whole system except the listed programs (first of all Argus itself: its voice-over
            // must not get into the "system audio" and return to recognition).
            let excluded = content.applications.filter { options.excludePids.contains($0.processID) }
            if excluded.count < options.excludePids.count {
                // The system may not have listed a program without windows; then its audio will not be
                // filtered out.
                emit(["event": "warning",
                      "message": "programs to exclude: asked \(options.excludePids.count), found \(excluded.count)"])
            }
            filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])
        } else {
            let included = content.applications.filter { options.pids.contains($0.processID) }
            guard !included.isEmpty else {
                throw CaptureError.noApplications
            }
            filter = SCContentFilter(display: display, including: included, exceptingWindows: [])
        }

        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = options.rate
        configuration.channelCount = options.channels
        // We do not need the picture, but a stream cannot exist without it: we ask for the
        // smallest and the rarest.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.showsCursor = false

        let newStream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try newStream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try newStream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await newStream.startCapture()
        remember(newStream)
    }

    /// Remember the stream. As a separate ordinary method: a lock cannot be taken directly in
    /// an `async` function (Swift 6 treats that as an error), but it can be from an ordinary
    /// function.
    private func remember(_ stream: SCStream) {
        streamLock.lock()
        activeStream = stream
        streamLock.unlock()
    }

    func stop(then done: @escaping () -> Void) {
        streamLock.lock()
        let current = activeStream
        activeStream = nil
        streamLock.unlock()
        guard let running = current else {
            done()
            return
        }
        running.stopCapture { _ in done() }
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        // Picture frames arrive but are not needed.
        guard type == .audio, CMSampleBufferIsValid(sampleBuffer), CMSampleBufferGetNumSamples(sampleBuffer) > 0 else {
            return
        }
        switch interleavedInt16(from: sampleBuffer, channels: options.channels, rate: options.rate) {
        case .audio(let pcm):
            do {
                try standardOutput.write(contentsOf: pcm)
            } catch {
                // There is no one to read (the pipe is closed): the owner is gone.
                finish(.ok, event: ["event": "stopped"])
            }
        case .skip(let why):
            // One chunk could not be parsed: no trouble, we say so once and stay silent afterwards.
            if !reportedSkip {
                reportedSkip = true
                emit(["event": "warning", "message": "skipped an audio chunk: \(why)"])
            }
        case .badFormat(let why):
            // The format is not the promised one: it will be the same later, and audio at a wrong
            // sample rate is worse than silence.
            finish(.failed, event: ["event": "error", "code": "format", "message": why])
        }
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        let (code, exitCode) = classify(error)
        finish(exitCode, event: ["event": "error", "code": code, "message": "\(error)"])
    }
}

/// What came out of one chunk of audio.
enum Converted {
    case audio(Data)         // ready audio for stdout
    case skip(String)        // the chunk is empty or could not be read: skip it; the text is needed for the log
    case badFormat(String)   // the format is not the promised one: it will be the same later, we cannot work
}

/// The audio of one chunk as int16, channels interleaved.
///
/// ScreenCaptureKit delivers Float32, usually separate per channel (its own buffer for each
/// channel), but it may also be interleaved (one buffer, several channels); we handle both.
/// It silently replaces the sample rate with 48 kHz if an unsupported one was requested, so
/// we compare the chunk's rate with the requested one.
func interleavedInt16(from sampleBuffer: CMSampleBuffer, channels wanted: Int, rate: Int) -> Converted {
    // The first call finds out what size of buffer list is needed; the second fills it.
    var sizeNeeded = 0
    var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        bufferListSizeNeededOut: &sizeNeeded,
        bufferListOut: nil,
        bufferListSize: 0,
        blockBufferAllocator: nil,
        blockBufferMemoryAllocator: nil,
        flags: 0,
        blockBufferOut: nil
    )
    guard status == noErr, sizeNeeded > 0 else { return .skip("buffer list size, status \(status)") }

    let raw = UnsafeMutableRawPointer.allocate(
        byteCount: sizeNeeded,
        alignment: MemoryLayout<AudioBufferList>.alignment
    )
    defer { raw.deallocate() }
    let list = raw.bindMemory(to: AudioBufferList.self, capacity: 1)

    var block: CMBlockBuffer?
    status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        bufferListSizeNeededOut: nil,
        bufferListOut: list,
        bufferListSize: sizeNeeded,
        blockBufferAllocator: nil,
        blockBufferMemoryAllocator: nil,
        flags: 0,
        blockBufferOut: &block
    )
    guard status == noErr else { return .skip("buffer list, status \(status)") }

    // While we read the buffers, the data block must stay alive.
    return withExtendedLifetime(block) { () -> Converted in
        guard
            let description = CMSampleBufferGetFormatDescription(sampleBuffer),
            let format = CMAudioFormatDescriptionGetStreamBasicDescription(description)?.pointee
        else {
            return .badFormat("no format description")
        }
        guard (format.mFormatFlags & kAudioFormatFlagIsFloat) != 0, format.mBitsPerChannel == 32 else {
            return .badFormat("audio is not 32-bit float")
        }
        guard abs(format.mSampleRate - Double(rate)) < 1.0 else {
            return .badFormat("sample rate is \(format.mSampleRate), wanted \(rate)")
        }
        let planar = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        let buffers = UnsafeMutableAudioBufferListPointer(list)
        guard buffers.count > 0 else { return .skip("no buffers") }

        let sourceChannels = planar ? buffers.count : Int(buffers[0].mNumberChannels)
        guard sourceChannels > 0 else { return .skip("no channels") }
        let bytesPerChannelBuffer = Int(buffers[0].mDataByteSize)
        let frames = planar
            ? bytesPerChannelBuffer / MemoryLayout<Float>.size
            : bytesPerChannelBuffer / (MemoryLayout<Float>.size * sourceChannels)
        guard frames > 0 else { return .skip("no frames") }

        var output = [Int16](repeating: 0, count: frames * wanted)
        for channel in 0..<wanted {
            // If the source has fewer channels than requested, the missing ones repeat the last one.
            let from = min(channel, sourceChannels - 1)
            let bufferIndex = planar ? from : 0
            guard let data = buffers[bufferIndex].mData else { return .skip("buffer without data") }
            let samples = data.assumingMemoryBound(to: Float.self)
            for frame in 0..<frames {
                let value = planar ? samples[frame] : samples[frame * sourceChannels + from]
                let clamped = max(-1.0, min(1.0, value))
                output[frame * wanted + channel] = Int16(clamped * 32767.0)
            }
        }
        return .audio(output.withUnsafeBufferPointer { Data(buffer: $0) })
    }
}

// MARK: - Launch

/// The probe result: an object, not a variable, because a captured variable cannot be
/// changed from a `Task`.
final class ProbeResult {
    var error: Error?
}

func runProbe() -> Never {
    let result = ProbeResult()
    let done = DispatchSemaphore(value: 0)
    Task {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            if content.displays.isEmpty {
                result.error = CaptureError.noDisplay
            }
        } catch {
            result.error = error
        }
        done.signal()
    }
    done.wait()
    if let error = result.error {
        let (code, exitCode) = classify(error)
        finish(exitCode, event: ["event": "error", "code": code, "message": "\(error)"])
    }
    finish(.ok, event: ["event": "probe", "ok": true])
}

// A write to a closed pipe must not kill the process with a signal: let it return an error.
signal(SIGPIPE, SIG_IGN)

let options = parseOptions()

if options.probe {
    runProbe()
}

let capture = SystemAudio(options: options)

// Exit on request: stdin was closed or SIGTERM/SIGINT arrived. Requests can come twice in
// a row (the app first closes stdin, a couple of seconds later sends SIGTERM), but we must
// exit only once.
let shutdownGate = DispatchSemaphore(value: 1)

func shutdown() {
    guard shutdownGate.wait(timeout: .now()) == .success else { return }
    let finished = DispatchSemaphore(value: 0)
    capture.stop { finished.signal() }
    // If the system does not respond, we do not wait forever.
    _ = finished.wait(timeout: .now() + 2)
    finish(.ok, event: ["event": "stopped"])
}

var signalSources: [DispatchSourceSignal] = []
for number in [SIGTERM, SIGINT] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { shutdown() }
    source.resume()
    signalSources.append(source)
}

DispatchQueue.global(qos: .utility).async {
    // A blocking read: an empty result or an error is the end of the stream, that is, the
    // owner closed stdin or died. `read(upToCount:)`, not `availableData`: on a read failure
    // the latter throws an Objective-C exception, which cannot be caught from Swift, and the
    // helper would crash with a crash report.
    while let chunk = try? FileHandle.standardInput.read(upToCount: 4096), !chunk.isEmpty {}
    shutdown()
}

Task {
    do {
        try await capture.start()
        emit(["event": "ready", "rate": options.rate, "channels": options.channels])
    } catch {
        let (code, exitCode) = classify(error)
        finish(exitCode, event: ["event": "error", "code": code, "message": "\(error)"])
    }
}

dispatchMain()
