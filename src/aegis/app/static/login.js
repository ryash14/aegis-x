const form = document.getElementById('login-form');
const button = document.getElementById('sign-in');
const error = document.getElementById('login-error');
let challenge;
async function prepare() {
  const response = await fetch('/api/auth/challenge');
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Could not prepare sign-in.');
  challenge = data.token;
}
form.addEventListener('submit', async event => {
  event.preventDefault();button.disabled = true;error.hidden = true;
  try {
    await prepare();
    const response = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json', 'X-Aegis-Login':challenge}, body:JSON.stringify({email:document.getElementById('email').value, password:document.getElementById('password').value})});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Sign-in failed.');
    document.getElementById('password').value = '';
    location.replace('/');
  } catch (failure) {error.textContent = failure.message;error.hidden = false;}
  finally {button.disabled = false;}
});
