// Minimal localStorage mock for Node-like environments in tests if needed.
// Playwright normally provides localStorage in browser contexts, but this can be
// used in helper code when running outside a browser context.

class LocalStorageMock {
  constructor() {
    this.store = {};
  }

  clear() {
    this.store = {};
  }

  getItem(key) {
    return this.store[key] || null;
  }

  setItem(key, value) {
    this.store[key] = String(value);
  }

  removeItem(key) {
    delete this.store[key];
  }
}

// Only install if not present
if (typeof global.localStorage === 'undefined') {
  global.localStorage = new LocalStorageMock();
}

export default LocalStorageMock;
