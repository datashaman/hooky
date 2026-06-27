// Helper utilities for localStorage setup/cleanup in tests.
// These are exposed for use in test files that need to seed or inspect storage.

export function clearTodosStorage() {
  localStorage.removeItem('todos');
}

export function setTodosStorage(items) {
  localStorage.setItem('todos', JSON.stringify(items));
}

export function getTodosStorage() {
  const raw = localStorage.getItem('todos');
  return raw ? JSON.parse(raw) : null;
}

export function createTodoItem(title, completed = false, id = Date.now().toString()) {
  return { id, title, completed };
}

export default {
  clearTodosStorage,
  setTodosStorage,
  getTodosStorage,
  createTodoItem,
};
