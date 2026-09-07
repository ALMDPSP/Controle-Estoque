document.addEventListener('DOMContentLoaded',()=>{
  const badge=document.querySelector('.scene-auth-badge');
  const form=document.querySelector('.login form');
  if(!badge||!form) return;
  form.addEventListener('submit',()=>{
    if(!form.checkValidity()) return;
    const progress=badge.dataset.progress;
    if(progress) badge.textContent=progress;
    badge.classList.add('is-processing');
  });
});
