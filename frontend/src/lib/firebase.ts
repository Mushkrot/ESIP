import { initializeApp } from 'firebase/app'
import { getAuth, GoogleAuthProvider } from 'firebase/auth'
import { REFERENCE_TENANT_DOMAIN } from '@/reference_data/fixtures'

// Values come from Firebase Console → Project Settings → Your apps → Web app.
// Copy frontend/.env.example → frontend/.env.local and fill them in.
const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY as string,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID as string,
  appId: import.meta.env.VITE_FIREBASE_APP_ID as string,
}

export const app = initializeApp(firebaseConfig)
export const auth = getAuth(app)

export const googleProvider = new GoogleAuthProvider()
// Hint the account chooser to show the configured synthetic tenant domain.
googleProvider.setCustomParameters({ hd: REFERENCE_TENANT_DOMAIN })
