const { test, expect } = require('@playwright/test');

test.describe('TodoMVC App', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('When there are no todos, the main todo list area and footer are hidden', async ({ page }) => {
    // Ensure localStorage is clean
    await page.evaluate(() => localStorage.clear());
    await page.reload();

    const todoList = page.locator('.todo-list');
    const footer = page.locator('footer');
    const mainSection = page.locator('section.main');

    await expect(todoList).toBeHidden();
    await expect(footer).toBeHidden();
    // main section may be present but empty; ensure list and footer are hidden
  });

  test('The new-todo input is focused on page load', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    await expect(newTodoInput).toBeFocused();
  });

  test('Pressing Enter in the new-todo input creates a todo only after trimming whitespace and rejecting empty input', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    // Reject empty input
    await newTodoInput.fill('');
    await newTodoInput.press('Enter');
    await expect(todoList.locator('li')).toHaveCount(0);

    // Reject whitespace-only input
    await newTodoInput.fill('   ');
    await newTodoInput.press('Enter');
    await expect(todoList.locator('li')).toHaveCount(0);

    // Create valid todo
    await newTodoInput.fill('Buy milk');
    await newTodoInput.press('Enter');
    await expect(todoList.locator('li')).toHaveCount(1);
    await expect(todoList.locator('li')).toContainText('Buy milk');

    // Trim whitespace around valid input
    await newTodoInput.fill('  Walk dog  ');
    await newTodoInput.press('Enter');
    await expect(todoList.locator('li')).toHaveCount(2);
    // Second item should be "Walk dog" trimmed
    const items = await todoList.locator('li label').allTextContents();
    expect(items).toContain('Walk dog');
  });

  test('The new-todo input is cleared after a todo is created', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    await newTodoInput.fill('Learn Playwright');
    await newTodoInput.press('Enter');
    await expect(newTodoInput).toHaveValue('');
  });

  test('The mark-all checkbox toggles all todos and reflects whether all individual todos are completed', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const toggleAll = page.locator('.toggle-all');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Task 1');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Task 2');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Task 3');
    await newTodoInput.press('Enter');

    // Initially mark-all should be unchecked (or indeterminate)
    // Toggle all to complete
    await toggleAll.check();
    const toggles = todoList.locator('.toggle');
    for (let i = 0; i < await toggles.count(); i++) {
      await expect(toggles.nth(i)).toBeChecked();
    }

    // Mark-all checkbox should reflect all completed
    await expect(toggleAll).toBeChecked();

    // Uncheck one todo
    await toggles.first().uncheck();
    // mark-all should no longer be checked (might become indeterminate or unchecked)
    // In TodoMVC, the "toggle all" checkbox reflects whether all are checked; when one is unchecked,
    // the toggle-all checkbox becomes unchecked.
    await expect(toggleAll).not.toBeChecked();

    // Re-check it to make all complete again
    await toggleAll.check();
    for (let i = 0; i < await toggles.count(); i++) {
      await expect(toggles.nth(i)).toBeChecked();
    }
  });

  test('Each todo can be marked complete or active individually via interaction with the todo item', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Task A');
    await newTodoInput.press('Enter');

    const item = todoList.locator('li').first();
    const toggle = item.locator('.toggle');
    const label = item.locator('label');

    // Initially active
    await expect(item).not.toHaveClass(/completed/);
    await expect(toggle).not.toBeChecked();

    // Mark complete
    await toggle.check();
    await expect(item).toHaveClass(/completed/);
    await expect(toggle).toBeChecked();

    // Mark active again
    await toggle.uncheck();
    await expect(item).not.toHaveClass(/completed/);
    await expect(toggle).not.toBeChecked();
  });

  test('Double-clicking a todo label enters editing mode and focuses the edit input', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Editable task');
    await newTodoInput.press('Enter');

    const item = todoList.locator('li').first();
    const label = item.locator('label');

    await label.dblclick();

    const editInput = item.locator('.edit');
    await expect(editInput).toBeVisible();
    await expect(editInput).toBeFocused();
    await expect(editInput).toHaveValue('Editable task');
  });

  test('Edits are saved on blur and on pressing Enter after trimming whitespace; empty edited title destroys the todo', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Original title');
    await newTodoInput.press('Enter');

    const item = todoList.locator('li').first();
    const label = item.locator('label');

    // Edit and save on blur
    await label.dblclick();
    const editInput = item.locator('.edit');
    await editInput.fill('Updated title');
    await editInput.blur();
    await expect(label).toHaveText('Updated title');

    // Edit and save on Enter
    await label.dblclick();
    const editInput2 = item.locator('.edit');
    await editInput2.fill('Another update');
    await editInput2.press('Enter');
    await expect(label).toHaveText('Another update');

    // Whitespace-only edit should destroy the todo
    await label.dblclick();
    const editInput3 = item.locator('.edit');
    await editInput3.fill('   ');
    await editInput3.press('Enter');
    // Item should be removed
    await expect(item).not.toBeVisible();
  });

  test('Escape while editing discards changes and exits editing mode', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Will not change');
    await newTodoInput.press('Enter');

    const item = todoList.locator('li').first();
    const label = item.locator('label');

    await label.dblclick();
    const editInput = item.locator('.edit');
    await editInput.fill('Discarded change');
    await editInput.press('Escape');

    // Should revert to original text
    await expect(label).toHaveText('Will not change');
    // Should not be in editing mode
    await expect(editInput).not.toBeVisible();
  });

  test('Hovering a todo shows its remove button', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Hover me');
    await newTodoInput.press('Enter');

    const item = todoList.locator('li').first();
    const destroyButton = item.locator('.destroy');

    // Destroy button may be hidden by default (display: none or opacity: 0)
    // Hover should make it visible
    await item.hover();
    await expect(destroyButton).toBeVisible();
  });

  test('The active counter is pluralized correctly: 0 items, 1 item, 2 items', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoCount = page.locator('.todo-count');

    // 0 items
    await expect(todoCount).toContainText('0 items');

    // 1 item
    await newTodoInput.fill('Single');
    await newTodoInput.press('Enter');
    // Wait for update
    await expect(todoCount).toContainText('1 item');

    // 2 items
    await newTodoInput.fill('Double');
    await newTodoInput.press('Enter');
    await expect(todoCount).toContainText('2 items');

    // Complete one -> 1 active item left
    const todoList = page.locator('.todo-list');
    const toggle = todoList.locator('li').first().locator('.toggle');
    await toggle.check();
    await expect(todoCount).toContainText('1 item');
  });

  test('The clear-completed button is visible only when completed todos exist and removes completed todos when clicked', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const clearCompleted = page.locator('.clear-completed');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Task 1');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Task 2');
    await newTodoInput.press('Enter');

    // Initially no completed -> clear-completed may be hidden
    await expect(clearCompleted).not.toBeVisible();

    // Complete one
    await todoList.locator('li').first().locator('.toggle').check();
    await expect(clearCompleted).toBeVisible();

    // Click clear-completed
    await clearCompleted.click();
    await expect(todoList.locator('li')).toHaveCount(1);
    await expect(todoList.locator('li')).toContainText('Task 2');

    // After removal, clear-completed should be hidden again
    await expect(clearCompleted).not.toBeVisible();
  });

  test('Todos persist to localStorage using item fields compatible with id, title, and completed', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Persisted task');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Another task');
    await newTodoInput.press('Enter');

    // Mark one complete
    await todoList.locator('li').first().locator('.toggle').check();

    // Inspect localStorage
    const stored = await page.evaluate(() => {
      const data = localStorage.getItem('todos');
      return data ? JSON.parse(data) : null;
    });

    expect(stored).toBeTruthy();
    expect(Array.isArray(stored)).toBe(true);
    expect(stored.length).toBe(2);

    const first = stored.find((t) => t.title === 'Persisted task');
    expect(first).toBeTruthy();
    expect(first.title).toBe('Persisted task');
    expect(typeof first.completed).toBe('boolean');
    expect(first.completed).toBe(true);

    const second = stored.find((t) => t.title === 'Another task');
    expect(second.completed).toBe(false);

    // Reload and verify UI reflects persisted state
    await page.reload();
    await expect(todoList.locator('li')).toHaveCount(2);
    const items = todoList.locator('li');
    // Expect first item still completed
    await expect(items.nth(0).locator('.toggle')).toBeChecked();
    await expect(items.nth(1).locator('.toggle')).not.toBeChecked();
  });

  test('Editing state is not persisted', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Will edit');
    await newTodoInput.press('Enter');

    // Enter edit mode but do not save
    const item = todoList.locator('li').first();
    const label = item.locator('label');
    await label.dblclick();
    const editInput = item.locator('.edit');
    await editInput.fill('Modified unsaved');

    // Reload page
    await page.reload();

    // The item should show original title (or last saved title), not the unsaved edit
    // Since we never saved, the stored title remains 'Will edit'
    await expect(label).toHaveText('Will edit');
    // Should not be in editing mode
    await expect(editInput).not.toBeVisible();
  });

  test('Routes are supported for all (#/), active (#/active), and completed (#/completed) filters', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Task A');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Task B');
    await newTodoInput.press('Enter');

    // Mark one completed
    await todoList.locator('li').first().locator('.toggle').check();

    // All route
    await page.goto('/#/');
    await expect(todoList.locator('li')).toHaveCount(2);

    // Active route
    await page.goto('/#/active');
    await expect(todoList.locator('li')).toHaveCount(1);
    await expect(todoList.locator('li')).toContainText('Task B');

    // Completed route
    await page.goto('/#/completed');
    await expect(todoList.locator('li')).toHaveCount(1);
    await expect(todoList.locator('li')).toContainText('Task A');
  });

  test('Changing routes filters the todo list and updates the selected filter link', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');
    const filters = page.locator('.filters a');

    await newTodoInput.fill('Task 1');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Task 2');
    await newTodoInput.press('Enter');
    await todoList.locator('li').first().locator('.toggle').check();

    const allLink = filters.filter({ hasText: 'All' });
    const activeLink = filters.filter({ hasText: 'Active' });
    const completedLink = filters.filter({ hasText: 'Completed' });

    // Start at All
    await expect(allLink).toHaveClass(/selected/);

    // Go to Active
    await activeLink.click();
    await expect(activeLink).toHaveClass(/selected/);
    await expect(allLink).not.toHaveClass(/selected/);
    await expect(todoList.locator('li')).toHaveCount(1);

    // Go to Completed
    await completedLink.click();
    await expect(completedLink).toHaveClass(/selected/);
    await expect(activeLink).not.toHaveClass(/selected/);
    await expect(todoList.locator('li')).toHaveCount(1);
  });

  test('If a todo changes state while a route filter is applied, the visible list updates immediately', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');
    const todoList = page.locator('.todo-list');

    await newTodoInput.fill('Alpha');
    await newTodoInput.press('Enter');
    await newTodoInput.fill('Beta');
    await newTodoInput.press('Enter');

    // Start on Active route
    await page.goto('/#/active');
    await expect(todoList.locator('li')).toHaveCount(2);

    // Complete one -> should disappear from active view
    await todoList.locator('li').first().locator('.toggle').check();
    await expect(todoList.locator('li')).toHaveCount(1);
    await expect(todoList.locator('li')).toContainText('Beta');

    // Switch to Completed and verify it's there
    await page.goto('/#/completed');
    await expect(todoList.locator('li')).toHaveCount(1);
    await expect(todoList.locator('li')).toContainText('Alpha');
  });

  test('The active route/filter persists across reloads', async ({ page }) => {
    const newTodoInput = page.locator('.new-todo');

    await newTodoInput.fill('Persisted route');
    await newTodoInput.press('Enter');

    await page.goto('/#/active');
    await expect(page).toHaveURL(/.*#\/active/);

    // Reload
    await page.reload();
    await expect(page).toHaveURL(/.*#\/active/);

    // Check that the Active link is selected
    const activeLink = page.locator('.filters a').filter({ hasText: 'Active' });
    await expect(activeLink).toHaveClass(/selected/);
  });
});
