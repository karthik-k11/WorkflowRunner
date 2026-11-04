(() => {
  const root = document.documentElement;
  const savedTheme = localStorage.getItem("workflowrunner-theme");
  if (savedTheme === "dark") root.dataset.theme = "dark";

  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      localStorage.setItem("workflowrunner-theme", root.dataset.theme);
    });
  });

  document.querySelectorAll("[data-menu-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById("sidebar")?.classList.toggle("sidebar-open");
    });
  });

  document.querySelectorAll(".flash-close").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll(".file-button input[type=file]").forEach((input) => {
    input.addEventListener("change", () => {
      const label = input.closest(".file-button");
      if (label && input.files?.[0]) label.firstChild.textContent = `${input.files[0].name} `;
    });
  });

  const editor = document.getElementById("workflow-editor");
  if (!editor) return;
  const taskList = document.getElementById("task-list");
  const hiddenTasks = document.getElementById("tasks-json");
  const errorBox = document.getElementById("editor-message");
  const initialData = document.getElementById("initial-task-data");
  let tasks = [];
  try {
    tasks = initialData ? JSON.parse(initialData.textContent || "[]") : [];
  } catch {
    errorBox.textContent = "The saved task data could not be loaded.";
  }

  const makeId = () => {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID().slice(0, 8);
    return `task-${Math.random().toString(36).slice(2, 8)}`;
  };
  const field = (labelText, control, wide = false) => {
    const label = document.createElement("label");
    label.className = `field${wide ? " field-wide" : ""}`;
    label.append(document.createTextNode(labelText), control);
    return label;
  };
  const textInput = (value, placeholder = "") => {
    const control = document.createElement("input");
    control.type = "text";
    control.value = value ?? "";
    control.placeholder = placeholder;
    control.maxLength = 1000;
    return control;
  };
  const selectInput = (values, selected, placeholder = "") => {
    const control = document.createElement("select");
    if (placeholder) {
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = placeholder;
      control.append(blank);
    }
    values.forEach(([value, name]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = name;
      control.append(option);
    });
    control.value = selected ?? "";
    return control;
  };
  const sourceValue = (value) => {
    if (value && typeof value === "object" && value.from_task) {
      return { source: value.from_task, value: "", path: value.path || "value" };
    }
    return { source: "", value: value == null ? "" : String(value), path: "value" };
  };
  const setMapped = (config, key, source, value, path = "value") => {
    if (source) return { from_task: source, path };
    if (config[key] && typeof config[key] === "object" && config[key].from_task) {
      return value;
    }
    return value;
  };

  function syncDependencyOptions() {
    const cards = [...taskList.querySelectorAll(".task-card")];
    cards.forEach((card) => {
      const select = card.querySelector(".dependency-select");
      const current = new Set([
        ...[...select.selectedOptions].map((option) => option.value),
        ...(card._dependencies || []),
      ]);
      const ownId = card.querySelector(".task-id").value.trim();
      select.replaceChildren();
      cards.forEach((candidate) => {
        const id = candidate.querySelector(".task-id").value.trim();
        if (!id || id === ownId) return;
        const option = document.createElement("option");
        option.value = id;
        option.textContent = `${candidate.querySelector(".task-name").value || "Untitled"} (${id})`;
        option.selected = current.has(id);
        select.append(option);
      });
      card._dependencies = [...select.selectedOptions].map((option) => option.value);
    });
  }

  function renderConfig(card, config = {}) {
    const area = card.querySelector(".task-config-fields");
    area.replaceChildren();
    const type = card.querySelector(".task-type").value;
    const operation = config.operation || "echo";
    if (type === "python") {
      const op = selectInput(
        [["echo", "Return a value"], ["add", "Add numbers"], ["uppercase", "Uppercase text"], ["count", "Count items"]],
        operation,
      );
      area.append(field("Application operation", op));
      const inputKey = operation === "add" ? "values" : operation === "count" ? "items" : "value";
      let existing = (config.inputs || {})[inputKey];
      if (existing === undefined) existing = config[inputKey];
      const mapped = sourceValue(existing);
      const source = selectInput(
        [...taskList.querySelectorAll(".task-card")].filter((other) => other !== card).map((other) => {
          const id = other.querySelector(".task-id").value.trim();
          return [id, `${other.querySelector(".task-name").value || id} · ${id}`];
        }),
        mapped.source,
        "Use literal value",
      );
      const inputValue = textInput(mapped.value, operation === "add" || operation === "count" ? "Comma-separated values" : "Value or text");
      if (operation === "add" || operation === "count") inputValue.dataset.list = "true";
      area.append(field(operation === "add" || operation === "count" ? "Input values" : "Input value", inputValue));
      area.append(field("Map from task output", source, true));
      source.addEventListener("change", () => {
        card._dependencies = [...new Set([...(card._dependencies || []), source.value].filter(Boolean))];
        syncDependencyOptions();
      });
      card._python = { op, inputKey, source, inputValue, original: config, originalValue: existing };
      op.addEventListener("change", () => {
        card._taskConfig = config;
        renderConfig(card, { operation: op.value });
      });
    } else if (type === "file") {
      const action = selectInput(
        [["exists", "Check whether a file exists"], ["read_text", "Read a permitted text file"], ["copy", "Copy a text file"]],
        config.action || "exists",
      );
      area.append(field("File operation", action));
      const source = textInput(config.source, "example.txt");
      const destination = textInput(config.destination, "archive/copy.txt");
      const path = textInput(config.path, "notes.txt");
      if (action.value === "copy") {
        area.append(field("Source path", source), field("Destination path", destination));
      } else {
        area.append(field("Path relative to file root", path, true));
      }
      card._file = { action, source, destination, path };
      action.addEventListener("change", () => renderConfig(card, { action: action.value }));
    } else {
      const command = selectInput(
        [["python-version", "Python version"], ["git-version", "Git version"]],
        config.command || "python-version",
      );
      area.append(field("Allowlisted command", command, true));
      card._command = command;
    }

    const retry = config.retry || {};
    const attempts = document.createElement("input");
    attempts.type = "number";
    attempts.min = "1";
    attempts.max = "5";
    attempts.value = retry.attempts || 1;
    const retryDelay = document.createElement("input");
    retryDelay.type = "number";
    retryDelay.min = "0";
    retryDelay.max = "5";
    retryDelay.step = "0.1";
    retryDelay.value = retry.delay_seconds || 0;
    const condition = config.condition || {};
    const when = selectInput(
      [["always", "Always run"], ["truthy", "When prerequisite output is truthy"], ["equals", "When output equals value"], ["not_equals", "When output differs from value"]],
      condition.operator ? condition.operator : "always",
    );
    const right = textInput(condition.right, "Expected value");
    const mappedCondition = sourceValue(condition.left);
    const conditionSource = selectInput(
      [...taskList.querySelectorAll(".task-card")].filter((other) => other !== card).map((other) => {
        const id = other.querySelector(".task-id").value.trim();
        return [id, `${other.querySelector(".task-name").value || id} · ${id}`];
      }),
      mappedCondition.source,
      "Use a literal condition value",
    );
    const conditionValue = textInput(mappedCondition.value || "true", "Condition value");
    area.append(
      field("Attempts (including first try)", attempts),
      field("Delay between retries (seconds)", retryDelay),
      field("Condition", when),
    );
    const sourceField = field("Condition source", conditionSource, true);
    const literalField = field("Literal condition value", conditionValue, true);
    const rightField = field("Compare with", right, true);
    sourceField.hidden = when.value === "always";
    literalField.hidden = when.value === "always" || Boolean(conditionSource.value);
    rightField.hidden = when.value !== "equals" && when.value !== "not_equals";
    area.append(sourceField, literalField, rightField);
    card._retry = { attempts, delay: retryDelay };
    card._condition = {
      when,
      right,
      conditionSource,
      conditionValue,
      sourceField,
      literalField,
      rightField,
      original: condition,
    };
    when.addEventListener("change", () => {
      sourceField.hidden = when.value === "always";
      literalField.hidden = when.value === "always" || Boolean(conditionSource.value);
      rightField.hidden = when.value !== "equals" && when.value !== "not_equals";
    });
    conditionSource.addEventListener("change", () => {
      literalField.hidden = when.value === "always" || Boolean(conditionSource.value);
      card._dependencies = [...new Set([...(card._dependencies || []), conditionSource.value].filter(Boolean))];
      syncDependencyOptions();
    });
  }

  function addTask(task = {}, deferDependencies = false) {
    const card = document.createElement("article");
    card.className = "task-card";
    const head = document.createElement("div");
    head.className = "task-card-head";
    const title = document.createElement("div");
    title.className = "task-card-title";
    const badge = document.createElement("span");
    badge.className = "task-order-badge";
    badge.textContent = String(taskList.children.length + 1).padStart(2, "0");
    const heading = document.createElement("strong");
    heading.textContent = task.name || "New task";
    title.append(badge, heading);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "small-action danger-text";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove task ${task.name || ""}`);
    head.append(title, remove);

    const basic = document.createElement("div");
    basic.className = "task-basic-grid";
    const id = textInput(task.task_id || makeId(), "task-id");
    id.className = "task-id mono";
    const name = textInput(task.name, "Task name");
    name.className = "task-name";
    const type = selectInput([["python", "Python operation"], ["file", "File operation"], ["command", "Command"]], task.task_type || "python");
    type.className = "task-type";
    const timeout = document.createElement("input");
    timeout.type = "number";
    timeout.min = "1";
    timeout.max = "300";
    timeout.value = task.timeout_seconds || 30;
    timeout.className = "task-timeout";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = task.enabled !== false;
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "toggle-field";
    enabledLabel.append(enabled, document.createTextNode(" Enabled"));
    basic.append(
      field("Task ID", id),
      field("Name", name),
      field("Task type", type),
      field("Timeout (seconds)", timeout),
      field("Depends on", Object.assign(document.createElement("select"), { multiple: true, className: "dependency-select" }), true),
      enabledLabel,
    );
    const configFields = document.createElement("div");
    configFields.className = "task-config-fields task-config-grid";
    card.append(head, basic, configFields);
    card._taskConfig = task.config || {};
    card._dependencies = task.dependencies || [];
    taskList.append(card);
    if (!deferDependencies) {
      renderConfig(card, task.config || {});
      syncDependencyOptions();
    }

    remove.addEventListener("click", () => {
      card.remove();
      [...taskList.querySelectorAll(".task-order-badge")].forEach((node, index) => {
        node.textContent = String(index + 1).padStart(2, "0");
      });
      syncDependencyOptions();
    });
    name.addEventListener("input", () => {
      heading.textContent = name.value || "New task";
      syncDependencyOptions();
    });
    id.addEventListener("input", syncDependencyOptions);
    type.addEventListener("change", () => renderConfig(card, {}));
    if (!deferDependencies) syncDependencyOptions();
  }

  function readCard(card) {
    const type = card.querySelector(".task-type").value;
    const config = {};
    if (type === "python") {
      const controls = card._python;
      config.operation = controls.op.value;
      let value = controls.inputValue.value;
      if (controls.inputValue.dataset.list) {
        value = value ? value.split(",").map((part) => {
          const trimmed = part.trim();
          const numeric = Number(trimmed);
          return trimmed !== "" && Number.isFinite(numeric) ? numeric : trimmed;
        }) : [];
      }
      if (
        !controls.source.value
        && !controls.inputValue.dataset.list
        && controls.originalValue !== undefined
        && !(controls.originalValue && typeof controls.originalValue === "object" && controls.originalValue.from_task)
        && String(controls.originalValue) === controls.inputValue.value
      ) {
        value = controls.originalValue;
      }
      const originalSource = controls.originalValue?.from_task;
      const path = originalSource === controls.source.value
        ? controls.originalValue.path || "value"
        : "value";
      config.inputs = { [controls.inputKey]: setMapped(controls.original, controls.inputKey, controls.source.value, value, path) };
    } else if (type === "file") {
      const controls = card._file;
      config.action = controls.action.value;
      if (config.action === "copy") {
        config.source = controls.source.value;
        config.destination = controls.destination.value;
      } else {
        config.path = controls.path.value;
      }
    } else {
      config.command = card._command.value;
    }

    const attempts = Number(card._retry.attempts.value || 1);
    const delay = Number(card._retry.delay.value || 0);
    if (attempts > 1) config.retry = { attempts, delay_seconds: delay };
    const condition = card._condition;
    if (condition.when.value !== "always") {
      const operator = condition.when.value;
      const originalLeft = condition.original?.left;
      const path = originalLeft?.from_task === condition.conditionSource.value
        ? originalLeft.path || "value"
        : "value";
      config.condition = {
        operator,
        left: condition.conditionSource.value
          ? { from_task: condition.conditionSource.value, path }
          : condition.conditionValue.value,
      };
      if (operator === "equals" || operator === "not_equals") config.condition.right = condition.right.value;
    }
    return {
      task_id: card.querySelector(".task-id").value.trim(),
      name: card.querySelector(".task-name").value.trim(),
      task_type: type,
      config,
      dependencies: [...new Set([
        ...[...card.querySelector(".dependency-select").selectedOptions].map((option) => option.value),
        card._python?.source.value,
        card._condition?.conditionSource.value,
      ].filter(Boolean))],
      timeout_seconds: Number(card.querySelector(".task-timeout").value),
      enabled: card.querySelector(".toggle-field input").checked,
    };
  }

  document.getElementById("add-task").addEventListener("click", () => addTask());
  editor.addEventListener("submit", (event) => {
    errorBox.textContent = "";
    try {
      const definitions = [...taskList.querySelectorAll(".task-card")].map(readCard);
      const ids = definitions.map((task) => task.task_id);
      if (ids.some((id) => !id)) throw new Error("Every task needs a task ID.");
      if (new Set(ids).size !== ids.length) throw new Error("Task IDs must be unique.");
      if (definitions.some((task) => !task.name)) throw new Error("Every task needs a name.");
      if (definitions.some((task) => !Number.isInteger(task.timeout_seconds) || task.timeout_seconds < 1 || task.timeout_seconds > 300)) {
        throw new Error("Task timeouts must be between 1 and 300 seconds.");
      }
      hiddenTasks.value = JSON.stringify(definitions);
    } catch (error) {
      event.preventDefault();
      errorBox.textContent = error instanceof Error ? error.message : "Task configuration is invalid.";
      errorBox.focus?.();
    }
  });

  tasks.forEach((task) => addTask(task, true));
  syncDependencyOptions();
  [...taskList.querySelectorAll(".task-card")].forEach((card) => {
    renderConfig(card, card._taskConfig);
  });
})();
