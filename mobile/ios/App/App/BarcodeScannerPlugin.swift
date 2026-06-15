//
//  BarcodeScannerPlugin.swift
//  Bitaxe Baller — iOS companion
//
//  Why this file exists
//  --------------------
//  We use @capacitor-mlkit/barcode-scanning on Android (where it works
//  perfectly) but its iOS module ships only via a CocoaPods .podspec. Our
//  Capacitor 8 project uses Swift Package Manager exclusively (no Podfile,
//  no CocoaPods). When `npx cap sync ios` runs against an SPM project, it
//  silently skips any plugin that doesn't expose a Package.swift — which
//  is exactly the case for @capacitor-mlkit/barcode-scanning v8.1.0.
//  Result: the JS layer's call to `BarcodeScanner.scan({ formats: ['QR_CODE'] })`
//  hits Capacitor's bridge → no native implementation registered → the
//  error message "BarcodeScanner plugin is not implemented on ios".
//
//  This file is the iOS-native implementation. It registers under the
//  same plugin name ("BarcodeScanner") that the MLKit plugin uses on
//  Android, returns the same response shape (barcodes array with rawValue
//  and format on each entry), and accepts the same scan options (formats
//  filter). The JS code in mobile/www/index.html doesn't change.
//
//  Implementation uses AVFoundation's AVCaptureMetadataOutput, which has
//  native QR detection built into iOS — no external dependency, no
//  framework size cost, and decoding happens entirely on-device.
//

import Foundation
import Capacitor
import AVFoundation
import UIKit

@objc(BarcodeScannerPlugin)
public class BarcodeScannerPlugin: CAPPlugin, CAPBridgedPlugin {
    // CAPBridgedPlugin requirements. `jsName` is what the JS layer
    // references when it calls `Capacitor.Plugins.BarcodeScanner.<method>`
    // — must match the MLKit plugin's name exactly so the existing
    // www/index.html JS works unchanged.
    public let identifier = "BarcodeScannerPlugin"
    public let jsName = "BarcodeScanner"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "scan", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "checkPermissions", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "requestPermissions", returnType: CAPPluginReturnPromise),
    ]

    // Keep a reference to the active scanner VC so we can dismiss it
    // cleanly on cancel and prevent multiple concurrent scans.
    private var activeScannerVC: ScannerViewController?

    // MARK: - Permissions
    //
    // checkPermissions / requestPermissions are declared as `open` on
    // CAPPlugin's base class so the bridge can resolve them generically.
    // We override them with `public` (matching base visibility) and
    // explicit `override` (the Swift compiler will reject the file
    // otherwise — caught in the v1.2.1 build).

    @objc public override func checkPermissions(_ call: CAPPluginCall) {
        call.resolve(["camera": authStatusString()])
    }

    @objc public override func requestPermissions(_ call: CAPPluginCall) {
        let status = AVCaptureDevice.authorizationStatus(for: .video)
        if status == .notDetermined {
            AVCaptureDevice.requestAccess(for: .video) { _ in
                DispatchQueue.main.async {
                    call.resolve(["camera": self.authStatusString()])
                }
            }
            return
        }
        call.resolve(["camera": authStatusString()])
    }

    private func authStatusString() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:    return "granted"
        case .denied:        return "denied"
        case .restricted:    return "denied"
        case .notDetermined: return "prompt"
        @unknown default:    return "prompt"
        }
    }

    // MARK: - Scan

    @objc func scan(_ call: CAPPluginCall) {
        // Permission gate. Mirrors MLKit behaviour: scan() will request
        // access if not yet determined, return error if denied.
        let status = AVCaptureDevice.authorizationStatus(for: .video)
        switch status {
        case .authorized:
            DispatchQueue.main.async { self.presentScanner(call: call) }
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { granted in
                DispatchQueue.main.async {
                    if granted {
                        self.presentScanner(call: call)
                    } else {
                        call.reject("Camera permission was denied")
                    }
                }
            }
        case .denied, .restricted:
            call.reject("Camera permission was denied")
        @unknown default:
            call.reject("Camera permission status unknown")
        }
    }

    private func presentScanner(call: CAPPluginCall) {
        guard activeScannerVC == nil else {
            call.reject("A scan is already in progress")
            return
        }
        // Accept a `formats` option — same shape MLKit expects. We only
        // support QR codes natively here; ignore any other format the
        // caller asks for since QR is the only one Bitaxe Baller's pair
        // flow uses.
        let formats = (call.getArray("formats", String.self) ?? ["QR_CODE"])
        let allowQR = formats.contains("QR_CODE")
        guard allowQR else {
            call.resolve(["barcodes": []])
            return
        }

        let vc = ScannerViewController()
        vc.modalPresentationStyle = .fullScreen
        vc.onResult = { [weak self] result in
            // Dismiss must happen on main thread; result formatting
            // matches MLKit's BarcodeResult shape so the existing JS
            // (`barcodes[0].rawValue`) keeps working.
            DispatchQueue.main.async {
                vc.dismiss(animated: true) {
                    self?.activeScannerVC = nil
                    switch result {
                    case .scanned(let value):
                        call.resolve([
                            "barcodes": [[
                                "rawValue": value,
                                "displayValue": value,
                                "format": "QR_CODE",
                                "valueType": "TEXT",
                            ]]
                        ])
                    case .cancelled:
                        // Match MLKit: a cancel rejects with a specific
                        // errorMessage that our JS already checks for.
                        call.reject("scan canceled")
                    case .error(let msg):
                        call.reject(msg)
                    }
                }
            }
        }
        activeScannerVC = vc
        bridge?.viewController?.present(vc, animated: true)
    }
}

// MARK: - Scanner view controller

/// Internal full-screen camera UI. Self-contained — owns the AVCaptureSession,
/// the preview layer, the cancel button, and the metadata-output delegate.
/// Returns the first QR code it detects (or a cancel) via `onResult`.
private final class ScannerViewController: UIViewController,
                                          AVCaptureMetadataOutputObjectsDelegate {

    enum Result {
        case scanned(String)
        case cancelled
        case error(String)
    }

    var onResult: ((Result) -> Void)?

    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    // Once we've fired a result we stop processing further metadata
    // callbacks; the session might still emit before stopRunning takes
    // effect, and we don't want to deliver the same code twice.
    private var didFire = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        setupSession()
        setupChrome()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        if !session.isRunning {
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                self?.session.startRunning()
            }
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if session.isRunning {
            session.stopRunning()
        }
    }

    private func setupSession() {
        guard let device = AVCaptureDevice.default(for: .video) else {
            onResult?(.error("No camera available"))
            return
        }
        do {
            let input = try AVCaptureDeviceInput(device: device)
            session.beginConfiguration()
            if session.canAddInput(input) { session.addInput(input) }
            let output = AVCaptureMetadataOutput()
            if session.canAddOutput(output) {
                session.addOutput(output)
                output.setMetadataObjectsDelegate(self, queue: .main)
                // Must come after addOutput — the session has to know
                // what types the hardware can detect before we filter to
                // just QR. Filtering to .qr alone is the cheap path.
                output.metadataObjectTypes = [.qr]
            }
            session.commitConfiguration()
        } catch {
            onResult?(.error("Could not open camera: \(error.localizedDescription)"))
            return
        }
        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.layer.bounds
        view.layer.addSublayer(preview)
        previewLayer = preview
    }

    private func setupChrome() {
        // Translucent overlay with a centered viewfinder cutout would be
        // nicer, but a top cancel button + bottom hint is enough for the
        // pair flow and keeps this file small. Real Bitaxe users will be
        // pointing at a desktop screen with a single QR, not foraging for
        // it in clutter — the UI just needs to not feel broken.
        let cancel = UIButton(type: .system)
        cancel.setTitle("Cancel", for: .normal)
        cancel.setTitleColor(.white, for: .normal)
        cancel.titleLabel?.font = .systemFont(ofSize: 17, weight: .semibold)
        cancel.translatesAutoresizingMaskIntoConstraints = false
        cancel.backgroundColor = UIColor.black.withAlphaComponent(0.4)
        cancel.layer.cornerRadius = 8
        cancel.contentEdgeInsets = UIEdgeInsets(top: 8, left: 16, bottom: 8, right: 16)
        cancel.addTarget(self, action: #selector(didTapCancel), for: .touchUpInside)
        view.addSubview(cancel)

        let hint = UILabel()
        hint.text = "Point at the QR code on your desktop"
        hint.textColor = .white
        hint.textAlignment = .center
        hint.font = .systemFont(ofSize: 15, weight: .medium)
        hint.translatesAutoresizingMaskIntoConstraints = false
        hint.backgroundColor = UIColor.black.withAlphaComponent(0.4)
        hint.layer.cornerRadius = 8
        hint.layer.masksToBounds = true
        view.addSubview(hint)

        NSLayoutConstraint.activate([
            cancel.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
            cancel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
            hint.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24),
            hint.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            hint.heightAnchor.constraint(equalToConstant: 40),
            hint.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 24),
            hint.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -24),
        ])
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.layer.bounds
    }

    @objc private func didTapCancel() {
        guard !didFire else { return }
        didFire = true
        onResult?(.cancelled)
    }

    // MARK: AVCaptureMetadataOutputObjectsDelegate

    func metadataOutput(_ output: AVCaptureMetadataOutput,
                       didOutput metadataObjects: [AVMetadataObject],
                       from connection: AVCaptureConnection) {
        guard !didFire else { return }
        // First valid QR result wins. metadataObjects may contain
        // multiple readable codes per frame on dense screens; we take
        // the first .qr with a non-empty string value.
        for obj in metadataObjects {
            guard let readable = obj as? AVMetadataMachineReadableCodeObject,
                  readable.type == .qr,
                  let value = readable.stringValue, !value.isEmpty
            else { continue }
            didFire = true
            onResult?(.scanned(value))
            return
        }
    }
}
