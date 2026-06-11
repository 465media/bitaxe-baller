// Plugin bootstrap — imports the npm packages so they register their proxies
// on Capacitor.Plugins.X at module-load time. esbuild bundles this into a
// single www/plugins.js that we load via a <script> tag from index.html.
// Without this step the plugins are dead silent: their registerPlugin()
// calls only fire when the module is imported.
import { BiometricAuth } from '@aparajita/capacitor-biometric-auth';
import { Preferences } from '@capacitor/preferences';
import { BarcodeScanner } from '@capacitor-mlkit/barcode-scanning';
window.BB_BiometricAuth = BiometricAuth;
window.BB_Preferences = Preferences;
window.BB_BarcodeScanner = BarcodeScanner;
console.log('[bb] plugins loaded',
  'BiometricAuth=', typeof BiometricAuth,
  'Preferences=', typeof Preferences,
  'BarcodeScanner=', typeof BarcodeScanner);
