// Public configuration only. Copy apiKey and appId from the Firebase web app.
window.SPEED_DEMON_CONFIG = {
  backendUrl: ["localhost", "127.0.0.1"].includes(location.hostname)
    ? "http://127.0.0.1:8000" : "https://speed-demom.onrender.com",
  firebase: {
    apiKey: "AIzaSyCuYkEFI2qWqEAYpPjOpe2kHapd_mhB_EE",
    authDomain: "speed-demon-d6581.firebaseapp.com",
    projectId: "speed-demon-d6581",
    storageBucket: "speed-demon-d6581.firebasestorage.app",
    messagingSenderId: "825431751982",
    appId: "1:825431751982:web:9f4c7cc4c86b1d5a1f0ebf"
  }
};
