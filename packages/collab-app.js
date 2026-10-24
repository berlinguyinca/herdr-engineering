// Standalone entry for the public collaboration page (no dashboard code is served here).
import { mountCollab, completeSignInIfRedirected } from './collab-view.js';

completeSignInIfRedirected().finally(() => mountCollab(document.getElementById('view'), { surface: 'public' }));
