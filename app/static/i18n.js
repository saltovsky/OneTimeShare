/* OneTimeShare — client-side i18n
 *
 *  - Default language: Russian (as configured by the operator).
 *  - User can switch to English via the RU/EN switcher in the header.
 *  - Preference is stored in localStorage under `ots-lang`.
 *  - URL `?lang=ru|en` overrides on first load (so admins can deep-link).
 *  - Browser language is the final fallback.
 *
 *  HTML contract:
 *      <span data-i18n="login.welcome">С возвращением</span>
 *      <input data-i18n-placeholder="login.usernamePh" placeholder="...">
 *      <button data-i18n-title="theme.toggle" title="...">
 *      <span data-bytes="1572864">1.5 МБ</span>     <- re-formatted on lang change
 *      <span data-files-count="3">3</span>          <- pluralized on lang change
 *
 *  On every `setLang()` or `applyI18n()` a custom `i18n:change` event is
 *  dispatched on `document` so other components (e.g. the upload script
 *  re-rendering the file list) can react.
 */
(function () {
    'use strict';

    var SUPPORTED = ['ru', 'en'];
    var DEFAULT_LANG = 'ru';
    var STORAGE_KEY = 'ots-lang';

    /* -------------------------------------------------------------- */
    /*  Translations                                                  */
    /* -------------------------------------------------------------- */
    var T = {
        ru: {
            'app.tagline':           'Self-hosted burn-after-reading file sharing',
            'common.signIn':         'Войти',
            'common.signOut':        'Выйти',
            'common.admin':          'Админ',
            'common.copy':           'Копировать',
            'common.copied':         'Скопировано!',
            'common.total':          'Всего',
            'common.error':          'Ошибка',

            'login.welcome':         'С возвращением',
            'login.subtitle':        'Войдите, чтобы создавать ссылки. Получателям учётная запись не нужна.',
            'login.invalid':         'Неверное имя пользователя или пароль.',
            'login.forbidden':       'У вас нет прав для доступа к этой странице.',
            'login.username':        'Имя пользователя',
            'login.usernamePh':      'Введите имя пользователя',
            'login.password':        'Пароль',
            'login.passwordPh':      'Введите пароль',
            'login.submit':          'Войти',
            'login.contactAdmin':    'Нет доступа? Свяжитесь с администратором этого экземпляра.',

            'upload.title':          'Безопасный обмен файлами',
            'upload.subtitle':       'Загрузите файлы и получите одноразовую ссылку. Ссылка самоуничтожится после первого скачивания.',
            'upload.files':          'Файлы',
            'upload.clickToUpload':  'Нажмите, чтобы выбрать',
            'upload.orDragDrop':     'или перетащите файлы',
            'upload.fileTypes':      'Любой тип · Несколько файлов упакуются в ZIP',
            'upload.password':       'Пароль',
            'upload.passwordOptional':'необязательно',
            'upload.passwordHint':   'Получатель должен будет ввести этот пароль',
            'upload.passwordPh':     'Оставьте пустым, чтобы не защищать',
            'upload.generate':       'Создать ссылку',
            'upload.uploading':      'Загрузка…',
            'upload.generating':     'Генерация ссылки…',
            'upload.ready':          'Ваша ссылка готова',
            'upload.protected':      '🔒 защищено паролем',
            'upload.errorNoFiles':   'Пожалуйста, выберите хотя бы один файл.',
            'upload.createAnother':  'Создать ещё одну ссылку',
            'upload.chooseAnother':  'Создать ещё',

            'password.title':        'Требуется пароль',
            'password.subtitle':     'Эта ссылка защищена. Введите пароль, чтобы получить файлы.',
            'password.ph':           'Введите пароль',
            'password.unlock':       'Разблокировать',
            'password.wrong':        'Неверный пароль',

            'download.title':        'Готово к скачиванию',
            'download.warning':      'Нажмите кнопку ниже. <strong>Ссылка будет уничтожена</strong> сразу после начала скачивания.',
            'download.button':       'Скачать',
            'download.retryHint':    'Если загрузка прервётся, файлы останутся доступны для повторной попытки.',

            'admin.title':           'Панель администратора',
            'admin.subtitle':        'Ожидающие ссылки — живут до скачивания или отзыва.',
            'admin.tabPanel':        'Админ панель',
            'admin.tabUsers':        'Пользователи',
            'admin.tabSettings':     'Настройки',
            'admin.signedInAs':      'Вы вошли как',
            'admin.badgeAdmin':      'админ',
            'admin.statLinks':       'Ссылки',
            'admin.statFiles':       'Файлы',
            'admin.statSize':        'Общий размер',
            'admin.statProtected':   'Защищено',
            'admin.pending':         'Ожидающие ссылки',
            'admin.total':           'Всего',
            'admin.yes':             'Да',
            'admin.revoke':          'Отозвать',
            'admin.confirmRevoke':   'Удалить ссылку {id}… и все её файлы? Это действие необратимо.',
            'admin.empty':           'Нет ожидающих ссылок',
            'admin.emptyDesc':       'Все ссылки были скачаны или отозваны.',
            'admin.colLink':         'Ссылка',
            'admin.colCreated':      'Создана',
            'admin.colFiles':        'Файлы',
            'admin.colSize':         'Размер',
            'admin.colPassword':     'Пароль',
            'admin.colAction':       'Действие',
            'admin.colUploader':     'Загрузил',
            'admin.colRole':         'Роль',

            'admin.usersTitle':      'Пользователи',
            'admin.addUser':         'Добавить пользователя',
            'admin.addUserBtn':      'Добавить',
            'admin.confirmDeleteUser': 'Удалить пользователя {username}? Это действие необратимо.',

            'admin.footerTitle':     'Текст в подвале',
            'admin.footerHint':      'Отображается на публичных страницах загрузки и скачивания.',
            'admin.footerPh':        'Введите текст подвала…',
            'admin.footerPreview':   'Предпросмотр',
            'admin.footerSave':      'Сохранить',

            'error.404':             'Ссылка не найдена',
            'error.410':             'Срок ссылки истёк',
            'error.413':             'Файл слишком большой',
            'error.401':             'Не авторизован',
            'error.403':             'Доступ запрещён',
            'error.generic':         'Что-то пошло не так',
            'error.backHome':        'На главную',

            'theme.toggle':          'Сменить тему',
            'lang.ru':               'Русский',
            'lang.en':               'English',
        },
        en: {
            'app.tagline':           'Self-hosted burn-after-reading file sharing',
            'common.signIn':         'Sign in',
            'common.signOut':        'Sign out',
            'common.admin':          'Admin',
            'common.copy':           'Copy',
            'common.copied':         'Copied!',
            'common.total':          'Total',
            'common.error':          'Error',

            'login.welcome':         'Welcome back',
            'login.subtitle':        'Sign in to create share links. Recipients don\'t need an account.',
            'login.invalid':         'Invalid username or password.',
            'login.forbidden':       'You don\'t have permission to access that page.',
            'login.username':        'Username',
            'login.usernamePh':      'Enter your username',
            'login.password':        'Password',
            'login.passwordPh':      'Enter your password',
            'login.submit':          'Sign in',
            'login.contactAdmin':    'Need access? Contact the administrator of this instance.',

            'upload.title':          'Share files securely',
            'upload.subtitle':       'Upload one or more files and get a one-time link. The link self-destructs after the recipient downloads.',
            'upload.files':          'Files',
            'upload.clickToUpload':  'Click to upload',
            'upload.orDragDrop':     'or drag and drop',
            'upload.fileTypes':      'Any file type · Multiple files become a ZIP',
            'upload.password':       'Password',
            'upload.passwordOptional':'optional',
            'upload.passwordHint':   'Requires from recipient',
            'upload.passwordPh':     'Leave empty for no password',
            'upload.generate':       'Generate share link',
            'upload.uploading':      'Uploading…',
            'upload.generating':     'Generating link…',
            'upload.ready':          'Your share link is ready',
            'upload.protected':      '🔒 password protected',
            'upload.errorNoFiles':   'Please choose at least one file.',
            'upload.createAnother':  'Create another link',
            'upload.chooseAnother':  'Create another',

            'password.title':        'Password required',
            'password.subtitle':     'This link is protected. Enter the password to access the files.',
            'password.ph':           'Enter password',
            'password.unlock':       'Unlock',
            'password.wrong':        'Wrong password',

            'download.title':        'Ready to download',
            'download.warning':      'Click the button below. <strong>This link will be destroyed</strong> as soon as the download starts.',
            'download.button':       'Download now',
            'download.retryHint':    'If the download is interrupted, the files remain available for retry.',

            'admin.title':           'Admin panel',
            'admin.subtitle':        'Pending share links — live until downloaded or revoked.',
            'admin.tabPanel':        'Admin panel',
            'admin.tabUsers':        'Users',
            'admin.tabSettings':     'Settings',
            'admin.signedInAs':      'Signed in as',
            'admin.badgeAdmin':      'admin',
            'admin.statLinks':       'Links',
            'admin.statFiles':       'Files',
            'admin.statSize':        'Total size',
            'admin.statProtected':   'Protected',
            'admin.pending':         'Pending links',
            'admin.total':           'Total',
            'admin.yes':             'Yes',
            'admin.revoke':          'Revoke',
            'admin.confirmRevoke':   'Delete link {id}… and all its files? This cannot be undone.',
            'admin.empty':           'No pending links',
            'admin.emptyDesc':       'All share links have been downloaded or revoked.',
            'admin.colLink':         'Link',
            'admin.colCreated':      'Created',
            'admin.colFiles':        'Files',
            'admin.colSize':         'Size',
            'admin.colPassword':     'Password',
            'admin.colAction':       'Action',
            'admin.colUploader':     'Uploader',
            'admin.colRole':         'Role',

            'admin.usersTitle':      'Users',
            'admin.addUser':         'Add user',
            'admin.addUserBtn':      'Add',
            'admin.confirmDeleteUser': 'Delete user {username}? This cannot be undone.',

            'admin.footerTitle':     'Footer text',
            'admin.footerHint':      'Shown on public upload and download pages.',
            'admin.footerPh':        'Enter footer text…',
            'admin.footerPreview':   'Preview',
            'admin.footerSave':      'Save',

            'error.404':             'Link not found',
            'error.410':             'Link has expired',
            'error.413':             'File too large',
            'error.401':             'Not authenticated',
            'error.403':             'Access denied',
            'error.generic':         'Something went wrong',
            'error.backHome':        'Back to home',

            'theme.toggle':          'Toggle theme',
            'lang.ru':               'Russian',
            'lang.en':               'English',
        }
    };

    /* -------------------------------------------------------------- */
    /*  Localised helpers                                             */
    /* -------------------------------------------------------------- */
    var SIZE_UNITS = {
        ru: ['Б', 'КБ', 'МБ', 'ГБ'],
        en: ['B', 'KB', 'MB', 'GB']
    };

    function humanSize(n) {
        if (n == null || isNaN(n)) n = 0;
        var u = SIZE_UNITS[getLang()] || SIZE_UNITS[DEFAULT_LANG];
        var i = 0; var v = Number(n);
        if (v < 0) v = 0;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        var decimals = (v < 10 && i > 0) ? 2 : 1;
        return v.toFixed(decimals) + ' ' + u[i];
    }

    function pluralFiles(n) {
        var lang = getLang();
        if (lang === 'ru') {
            var mod10 = n % 10;
            var mod100 = n % 100;
            if (mod10 === 1 && mod100 !== 11) return n + ' файл';
            if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return n + ' файла';
            return n + ' файлов';
        }
        return n + ' file' + (n === 1 ? '' : 's');
    }

    function pluralProtected(n, total) {
        var lang = getLang();
        if (lang === 'ru') {
            var mod10 = n % 10;
            var mod100 = n % 100;
            if (mod10 === 1 && mod100 !== 11) return n + ' защищена';
            if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return n + ' защищены';
            return n + ' защищено';
        }
        return n + ' / ' + total;
    }

    /* -------------------------------------------------------------- */
    /*  Language resolution                                           */
    /* -------------------------------------------------------------- */
    function getLang() {
        try {
            var stored = localStorage.getItem(STORAGE_KEY);
            if (stored && SUPPORTED.indexOf(stored) !== -1) return stored;
        } catch (_) {}
        try {
            var url = new URL(window.location.href);
            var q = url.searchParams.get('lang');
            if (q && SUPPORTED.indexOf(q) !== -1) {
                try { localStorage.setItem(STORAGE_KEY, q); } catch (_) {}
                return q;
            }
        } catch (_) {}
        var browser = ((navigator.language || navigator.userLanguage || 'ru') + '').slice(0, 2).toLowerCase();
        if (SUPPORTED.indexOf(browser) !== -1) return browser;
        return DEFAULT_LANG;
    }

    function setLang(lang) {
        if (SUPPORTED.indexOf(lang) === -1) return;
        try { localStorage.setItem(STORAGE_KEY, lang); } catch (_) {}
        try {
            var url = new URL(window.location.href);
            if (url.searchParams.get('lang') !== null) {
                url.searchParams.delete('lang');
                window.history.replaceState({}, '', url.toString());
            }
        } catch (_) {}
        document.documentElement.lang = lang;
        applyI18n();
    }

    function t(key, params) {
        var lang = getLang();
        var dict = T[lang] || T[DEFAULT_LANG];
        var template = (dict && dict[key]) || (T[DEFAULT_LANG] && T[DEFAULT_LANG][key]) || key;
        if (params) {
            for (var k in params) {
                if (Object.prototype.hasOwnProperty.call(params, k)) {
                    template = template.replace(new RegExp('\\{' + k + '\\}', 'g'), params[k]);
                }
            }
        }
        return template;
    }

    /* -------------------------------------------------------------- */
    /*  DOM application                                               */
    /* -------------------------------------------------------------- */
    function applyI18n() {
        // text content (innerHTML — translations may contain trusted HTML like <strong>)
        var nodes = document.querySelectorAll('[data-i18n]');
        for (var i = 0; i < nodes.length; i++) {
            nodes[i].innerHTML = t(nodes[i].dataset.i18n);
        }
        // placeholders
        nodes = document.querySelectorAll('[data-i18n-placeholder]');
        for (var j = 0; j < nodes.length; j++) {
            nodes[j].placeholder = t(nodes[j].dataset.i18nPlaceholder);
        }
        // titles
        nodes = document.querySelectorAll('[data-i18n-title]');
        for (var k = 0; k < nodes.length; k++) {
            nodes[k].title = t(nodes[k].dataset.i18nTitle);
        }
        // aria-labels
        nodes = document.querySelectorAll('[data-i18n-aria]');
        for (var l = 0; l < nodes.length; l++) {
            nodes[l].setAttribute('aria-label', t(nodes[l].dataset.i18nAria));
        }
        // data-bytes (re-render with localised unit)
        nodes = document.querySelectorAll('[data-bytes]');
        for (var m = 0; m < nodes.length; m++) {
            var n = parseInt(nodes[m].dataset.bytes, 10);
            if (!isNaN(n)) nodes[m].textContent = humanSize(n);
        }
        // data-files-count (pluralised)
        nodes = document.querySelectorAll('[data-files-count]');
        for (var p = 0; p < nodes.length; p++) {
            var fc = parseInt(nodes[p].dataset.filesCount, 10);
            if (!isNaN(fc)) nodes[p].textContent = pluralFiles(fc);
        }
        // data-protected-count
        nodes = document.querySelectorAll('[data-protected-count]');
        for (var q = 0; q < nodes.length; q++) {
            var pc = parseInt(nodes[q].dataset.protectedCount, 10);
            var tc = parseInt(nodes[q].dataset.protectedTotal, 10) || pc;
            if (!isNaN(pc)) nodes[q].innerHTML = pluralProtected(pc, tc);
        }
        // update switcher visual state
        var langBtns = document.querySelectorAll('[data-lang]');
        for (var r = 0; r < langBtns.length; r++) {
            var active = langBtns[r].dataset.lang === getLang();
            langBtns[r].setAttribute('aria-pressed', active ? 'true' : 'false');
            if (active) {
                langBtns[r].className = langBtns[r].className
                    .replace('text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800/60', '')
                    .replace(/\s+/g, ' ').trim() + ' bg-indigo-600 dark:bg-indigo-500 text-white';
            } else {
                langBtns[r].className = langBtns[r].className
                    .replace('bg-indigo-600 dark:bg-indigo-500 text-white', '')
                    .replace(/\s+/g, ' ').trim() + ' text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800/60';
            }
        }
        // notify listeners (e.g. upload script re-renders file list)
        try {
            document.dispatchEvent(new CustomEvent('i18n:change', { detail: { lang: getLang() } }));
        } catch (_) {}
    }

    /* -------------------------------------------------------------- */
    /*  Initialisation                                                */
    /* -------------------------------------------------------------- */
    function init() {
        document.documentElement.lang = getLang();
        applyI18n();
        var langBtns = document.querySelectorAll('[data-lang]');
        for (var i = 0; i < langBtns.length; i++) {
            langBtns[i].addEventListener('click', function (e) {
                e.preventDefault();
                setLang(this.dataset.lang);
            });
        }
    }

    /* Expose public API for other scripts on the page */
    window.I18N = { t: t, getLang: getLang, setLang: setLang, humanSize: humanSize, pluralFiles: pluralFiles, applyI18n: applyI18n };

    /* Run before any visible content to avoid flash of wrong language */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
