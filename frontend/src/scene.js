import * as THREE from "three";

const stageDetails = [
  {
    title: "Movie Review Input",
    description: "A raw IMDb review enters the sentiment workflow.",
  },
  {
    title: "Text Preprocessing",
    description: "HTML is removed and review text is normalized and tokenized.",
  },
  {
    title: "TF-IDF / Tokenization",
    description: "The baseline vectorizes text; the neural path encodes tokens.",
  },
  {
    title: "Model Inference",
    description: "Saved Logistic Regression and TextCNN models score the review.",
  },
  {
    title: "Sentiment Output",
    description: "The selected model returns negative and positive probabilities.",
  },
];

const colors = {
  ink: 0x26342b,
  green: 0x159b62,
  darkGreen: 0x087747,
  mint: 0xd8f2e3,
  orange: 0xff9b45,
  coral: 0xf47768,
  yellow: 0xf4c95d,
  white: 0xffffff,
  line: 0xc6dfd0,
};

function createStageObjects(group, index) {
  const platform = new THREE.Mesh(
    new THREE.BoxGeometry(1.65, 0.16, 1.05),
    new THREE.MeshStandardMaterial({
      color: [colors.mint, 0xe7f4e9, 0xffead4, 0xffe3dc, 0xe0f4e7][index],
      roughness: 0.36,
      metalness: 0.04,
    }),
  );
  platform.position.y = -0.48;
  platform.castShadow = true;
  platform.receiveShadow = true;
  group.add(platform);

  if (index === 0) {
    const reel = new THREE.Group();
    const disk = new THREE.Mesh(
      new THREE.CylinderGeometry(0.43, 0.43, 0.11, 48),
      new THREE.MeshStandardMaterial({
        color: colors.ink,
        metalness: 0.62,
        roughness: 0.27,
      }),
    );
    disk.rotation.x = Math.PI / 2;
    reel.add(disk);

    const rim = new THREE.Mesh(
      new THREE.TorusGeometry(0.43, 0.055, 12, 48),
      new THREE.MeshStandardMaterial({
        color: 0xcbd5cf,
        metalness: 0.75,
        roughness: 0.22,
      }),
    );
    reel.add(rim);

    for (let holeIndex = 0; holeIndex < 6; holeIndex += 1) {
      const angle = (holeIndex / 6) * Math.PI * 2;
      const hole = new THREE.Mesh(
        new THREE.CylinderGeometry(0.07, 0.07, 0.13, 20),
        new THREE.MeshStandardMaterial({ color: 0xf1f4f1, roughness: 0.42 }),
      );
      hole.rotation.x = Math.PI / 2;
      hole.position.set(Math.cos(angle) * 0.25, Math.sin(angle) * 0.25, 0.02);
      reel.add(hole);
    }
    const hub = new THREE.Mesh(
      new THREE.CylinderGeometry(0.11, 0.11, 0.14, 32),
      new THREE.MeshStandardMaterial({ color: colors.yellow, metalness: 0.25 }),
    );
    hub.rotation.x = Math.PI / 2;
    hub.position.z = 0.02;
    reel.add(hub);
    reel.position.set(-0.27, 0.12, 0.13);
    reel.userData.isReel = true;
    group.add(reel);

    const reviewCard = new THREE.Mesh(
      new THREE.BoxGeometry(0.78, 0.62, 0.09),
      new THREE.MeshStandardMaterial({ color: colors.white, roughness: 0.31 }),
    );
    reviewCard.position.set(0.53, 0.18, 0.17);
    reviewCard.rotation.z = -0.11;
    reviewCard.castShadow = true;
    group.add(reviewCard);
    return;
  }

  const blockColors = [
    [colors.green, colors.mint, colors.yellow],
    [colors.green, colors.orange, colors.coral, colors.mint],
    [colors.orange, colors.coral, colors.green],
    [colors.green, colors.orange],
  ][index - 1];
  blockColors.forEach((color, itemIndex) => {
    const box = new THREE.Mesh(
      new THREE.BoxGeometry(
        index === 4 ? 0.65 : 0.42,
        index === 4 ? 0.42 : 0.38,
        0.38,
      ),
      new THREE.MeshStandardMaterial({
        color,
        roughness: 0.3,
        metalness: 0.06,
        emissive: color,
        emissiveIntensity: 0.035,
      }),
    );
    const column = itemIndex % 2;
    const row = Math.floor(itemIndex / 2);
    box.position.set((column - 0.5) * 0.46, 0.04 + row * 0.4, 0.12 + row * 0.03);
    box.rotation.y = (itemIndex - 1) * 0.08;
    box.castShadow = true;
    box.receiveShadow = true;
    group.add(box);
  });

  if (index === 4) {
    const negativeCard = new THREE.Mesh(
      new THREE.BoxGeometry(0.42, 0.24, 0.08),
      new THREE.MeshStandardMaterial({ color: colors.coral, roughness: 0.35 }),
    );
    negativeCard.position.set(0.0, 0.48, 0.28);
    group.add(negativeCard);
  }
}

function showFallback(root, message) {
  root.innerHTML = `<div class="scene-fallback" role="status">${message}</div>`;
}

function initializeScene() {
  const root = document.getElementById("scene-canvas");
  const note = document.getElementById("stage-note");
  const controls = [...document.querySelectorAll("[data-stage]")];
  if (!root || !note) return;

  if (!window.WebGLRenderingContext) {
    showFallback(
      root,
      "The 3D preview is unavailable in this browser. The five pipeline stages remain available below.",
    );
    return;
  }

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      alpha: true,
      antialias: true,
      powerPreference: "low-power",
    });
  } catch {
    showFallback(
      root,
      "WebGL is unavailable in this browser. The pipeline stages remain available below.",
    );
    return;
  }

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.setClearColor(0xffffff, 0);
  root.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 80);
  camera.position.set(0, 2.3, 9.0);
  camera.lookAt(0, 0.1, 0);

  scene.add(new THREE.HemisphereLight(0xffffff, 0xd9e8dd, 2.1));
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
  keyLight.position.set(-3, 7, 8);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(512, 512);
  scene.add(keyLight);
  const warmLight = new THREE.PointLight(colors.yellow, 18, 12);
  warmLight.position.set(4, 3, 3);
  scene.add(warmLight);

  const positions = [-5.0, -2.5, 0, 2.5, 5.0];
  const stageGroups = stageDetails.map((stage, index) => {
    const group = new THREE.Group();
    group.position.x = positions[index];
    group.userData.stageIndex = index;
    createStageObjects(group, index);
    scene.add(group);
    return group;
  });

  const connectors = [];
  for (let index = 0; index < positions.length - 1; index += 1) {
    const length = positions[index + 1] - positions[index] - 1.7;
    const connector = new THREE.Mesh(
      new THREE.CylinderGeometry(0.035, 0.035, length, 12),
      new THREE.MeshStandardMaterial({ color: colors.line, roughness: 0.45 }),
    );
    connector.rotation.z = -Math.PI / 2;
    connector.position.set((positions[index] + positions[index + 1]) / 2, -0.22, 0);
    scene.add(connector);
    connectors.push(connector);
  }

  const particleGeometry = new THREE.SphereGeometry(0.065, 14, 12);
  const particleMaterial = new THREE.MeshStandardMaterial({
    color: colors.green,
    emissive: colors.mint,
    emissiveIntensity: 0.6,
    roughness: 0.25,
  });
  const particles = Array.from({ length: 8 }, (_, index) => {
    const particle = new THREE.Mesh(particleGeometry, particleMaterial);
    particle.userData.offset = index / 8;
    particle.position.y = -0.2 + (index % 2) * 0.22;
    scene.add(particle);
    return particle;
  });

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const canvas = renderer.domElement;
  let selectedIndex = 0;
  let hoveredIndex = -1;
  let pointerX = 0;
  let pointerY = 0;
  let horizontalScale = 1;
  let animationFrame = 0;
  let visible = true;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function renderFrame(time = 0) {
    animationFrame = 0;
    const seconds = time * 0.001;
    camera.position.x += (pointerX * 0.5 - camera.position.x) * 0.025;
    camera.position.y += (2.3 + pointerY * 0.25 - camera.position.y) * 0.025;
    stageGroups.forEach((group, index) => {
      const targetScale = index === selectedIndex || index === hoveredIndex ? 1.055 : 1;
      group.position.x = positions[index] * horizontalScale;
      group.scale.lerp(
        new THREE.Vector3(horizontalScale * targetScale, targetScale, targetScale),
        0.08,
      );
      group.children.forEach((child) => {
        if (child.userData.isReel) child.rotation.z = seconds * 0.16;
      });
    });
    particles.forEach((particle) => {
      const progress = (seconds * 0.045 + particle.userData.offset) % 1;
      particle.position.x = (-5.8 + progress * 11.6) * horizontalScale;
      particle.position.y = -0.18 + Math.sin(seconds * 1.3 + progress * 12) * 0.11;
    });
    renderer.render(scene, camera);
    if (visible && !reduceMotion.matches) {
      animationFrame = window.requestAnimationFrame(renderFrame);
    }
  }

  function resizeScene() {
    const width = Math.max(1, root.clientWidth);
    const height = Math.max(1, root.clientHeight);
    camera.aspect = width / height;
    horizontalScale = THREE.MathUtils.clamp(camera.aspect / 2.2, 0.56, 1);
    stageGroups.forEach((group, index) => {
      group.position.x = positions[index] * horizontalScale;
      group.scale.x = horizontalScale;
    });
    connectors.forEach((connector, index) => {
      connector.position.x =
        ((positions[index] + positions[index + 1]) / 2) * horizontalScale;
      connector.scale.y = horizontalScale;
    });
    const visiblePipelineWidth = 11.65 * horizontalScale;
    const verticalFov = THREE.MathUtils.degToRad(34);
    camera.position.z = Math.max(
      8.4,
      visiblePipelineWidth / (2 * Math.tan(verticalFov / 2) * camera.aspect),
    );
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    if (!animationFrame) renderFrame();
  }

  function selectStage(index) {
    selectedIndex = index;
    note.textContent = stageDetails[index].description;
    controls.forEach((button, buttonIndex) => {
      const active = buttonIndex === index;
      button.setAttribute("aria-pressed", String(active));
      button.classList.toggle("is-active", active);
    });
    if (!animationFrame) renderFrame();
  }

  function updatePointer(event) {
    const bounds = canvas.getBoundingClientRect();
    pointerX = ((event.clientX - bounds.left) / bounds.width - 0.5) * 2;
    pointerY = ((event.clientY - bounds.top) / bounds.height - 0.5) * 2;
    pointer.set(pointerX, -pointerY);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(stageGroups, true)[0];
    hoveredIndex = hit
      ? stageGroups.findIndex((group) => group === hit.object.parent || group === hit.object)
      : -1;
    if (hit) {
      let object = hit.object;
      while (object && object.userData.stageIndex === undefined) object = object.parent;
      if (object) hoveredIndex = object.userData.stageIndex;
    }
    canvas.style.cursor = hit ? "pointer" : "default";
    if (!animationFrame) renderFrame();
  }

  function clickStage(event) {
    updatePointer(event);
    if (hoveredIndex >= 0) selectStage(hoveredIndex);
  }

  function onVisibilityChange() {
    visible = !document.hidden;
    if (!visible && animationFrame) {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    } else if (visible && !animationFrame) {
      renderFrame();
    }
  }

  const resizeObserver = new ResizeObserver(resizeScene);
  resizeObserver.observe(root);
  const intersectionObserver = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting && !document.hidden;
    if (!visible && animationFrame) {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    } else if (visible && !animationFrame) {
      renderFrame();
    }
  });
  intersectionObserver.observe(root);
  controls.forEach((button) => {
    const index = Number(button.dataset.stage);
    button.addEventListener("click", () => selectStage(index));
    button.addEventListener("focus", () => selectStage(index));
  });
  canvas.addEventListener("pointermove", updatePointer);
  canvas.addEventListener("pointerleave", () => {
    hoveredIndex = -1;
    pointerX = 0;
    pointerY = 0;
    canvas.style.cursor = "default";
    if (!animationFrame) renderFrame();
  });
  canvas.addEventListener("click", clickStage);
  document.addEventListener("visibilitychange", onVisibilityChange);
  reduceMotion.addEventListener("change", onVisibilityChange);
  selectStage(0);
  resizeScene();
  renderFrame();

  window.addEventListener(
    "pagehide",
    () => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
      resizeObserver.disconnect();
      intersectionObserver.disconnect();
      canvas.removeEventListener("pointermove", updatePointer);
      canvas.removeEventListener("click", clickStage);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      reduceMotion.removeEventListener("change", onVisibilityChange);
      scene.traverse((object) => {
        if (object.geometry) object.geometry.dispose();
        if (Array.isArray(object.material)) {
          object.material.forEach((material) => material.dispose());
        } else if (object.material) {
          object.material.dispose();
        }
      });
      renderer.dispose();
      renderer.domElement.remove();
    },
    { once: true },
  );
}

initializeScene();
