import { $el } from "/scripts/ui.js";

$el("style", {
  textContent: `
  .account-manager-logout {
    color: #ff453a !important;
    border-radius: 10px !important;
    transition: background-color 160ms ease, color 160ms ease, box-shadow 160ms ease;
  }
  
  .account-manager-logout:hover {
    background: rgba(255, 69, 58, 0.12) !important;
    color: #ff6961 !important;
    box-shadow: inset 0 0 0 1px rgba(255, 69, 58, 0.18);
  }

  #logout-menu-button {
    background: rgba(255, 69, 58, 0.1) !important;
    color: #ff453a !important;
    border-radius: 10px !important;
  }

  #logout-menu-button:hover {
    background: rgba(255, 69, 58, 0.16) !important;
    color: #ff6961 !important;
  }

  #logout-menu-button .logout-icon {
    margin: 8px 0 8px 10px;
    font-size: 15px;
  }
  `,
  parent: document.head,
});
