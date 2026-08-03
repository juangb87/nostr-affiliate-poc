from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

SUPPORTED_LANGUAGES = {"es", "en"}
DEFAULT_LANGUAGE = "es"
LANGUAGE_COOKIE = "meerat_lang"

# The Spanish copy is canonical. English is applied only to UI text nodes and
# translatable attributes; merchant-authored content and technical values stay intact.
ES_TO_EN: dict[str, str] = {
    "Entrar · Meerat": "Sign in · Meerat",
    "Tu red de afiliados, verificable": "Your verifiable affiliate network",
    "Entrá con tu identidad Nostr.": "Sign in with your Nostr identity.",
    "Sin contraseñas ni claves privadas compartidas. Tu extensión firma un desafío de un solo uso y Meerat crea una sesión segura.": "No passwords or shared private keys. Your extension signs a one-time challenge and Meerat creates a secure session.",
    "Sin contraseñas ni claves privadas compartidas. Firmá un desafío de un solo uso desde una extensión o una app Nostr en tu teléfono.": "No passwords or shared private keys. Sign a one-time challenge from a browser extension or a Nostr app on your phone.",
    "1 · Elegí tu espacio": "1 · Choose your workspace",
    "2 · Elegí cómo firmar": "2 · Choose how to sign",
    "Continuar con una app Nostr": "Continue with a Nostr app",
    "Usar extensión del navegador": "Use browser extension",
    "En mobile abrimos tu app Nostr. En desktop mostramos un QR para escanear con tu teléfono.": "On mobile, we open your Nostr app. On desktop, we show a QR code to scan with your phone.",
    "La firma no mueve fondos ni autoriza pagos. La conexión mobile es temporal y se elimina al terminar.": "The signature does not move funds or authorize payments. The mobile connection is temporary and is removed when finished.",
    "Acceso por invitación durante el POC.": "Invitation-only access during the POC.",
    "Tu identidad Nostr debe estar asociada previamente a un comerciante o a una inscripción de afiliado.": "Your Nostr identity must already be associated with a merchant or an affiliate enrollment.",
    "Cuenta de comerciante": "Merchant account",
    "Gestioná campañas, conversiones, afiliados y obligaciones de pago.": "Manage campaigns, conversions, affiliates, and payment obligations.",
    "Cuenta de afiliado": "Affiliate account",
    "Copiá tus enlaces, seguí conversiones y verificá tus ganancias.": "Copy your links, track conversions, and verify your earnings.",
    "Operaciones": "Operations",
    "Acceso restringido a diagnóstico, pruebas y operaciones técnicas.": "Restricted access for diagnostics, testing, and technical operations.",
    "Usá una extensión compatible con NIP-07, como Alby.": "Use a NIP-07-compatible extension, such as Alby.",
    "Ahora continuá con tu signer Nostr.": "Now continue with your Nostr signer.",
    "Elegí primero si entrás como comerciante o afiliado.": "First choose whether you are signing in as a merchant or affiliate.",
    "Preparando desafío seguro…": "Preparing secure challenge…",
    "Confirmá la firma en tu extensión Nostr…": "Confirm the signature in your Nostr extension…",
    "Conectá tu app Nostr y confirmá la firma…": "Connect your Nostr app and confirm the signature…",
    "Validando identidad…": "Validating identity…",
    "No encontramos una extensión Nostr. Usá una app Nostr o el QR para continuar.": "We could not find a Nostr extension. Use a Nostr app or the QR code to continue.",
    "No se pudo completar la operación. Intentá nuevamente.": "The operation could not be completed. Try again.",
    "Meerat nunca solicita tu nsec.": "Meerat never asks for your nsec.",
    "La firma no mueve fondos ni autoriza pagos.": "The signature does not move funds or authorize payments.",
    "Saltar al contenido": "Skip to content",
    "Cerrar sesión": "Sign out",
    "Navegación móvil": "Mobile navigation",
    "Navegación principal": "Main navigation",
    "Operativo": "Operational",
    "Espacio de trabajo": "Workspace",
    "Idioma": "Language",
    "Español": "Spanish",
    "Inglés": "English",
    "Configuración inicial del comerciante · Meerat": "Merchant setup · Meerat",
    "Configuración inicial del comerciante": "Merchant setup",
    "Configuración inicial": "Initial setup",
    "Configurá tu programa.": "Set up your program.",
    "Primero definimos tu marca, después las condiciones y finalmente la invitación que verá cada afiliado.": "First, we define your brand, then the terms, and finally the invitation each affiliate will see.",
    "Progreso de la configuración inicial": "Initial setup progress",
    "Marca": "Brand",
    "Cómo te van a reconocer": "How people will recognize you",
    "Programa": "Program",
    "Comisión y destino": "Commission and destination",
    "Invitación": "Invitation",
    "El mensaje para los afiliados": "The message for affiliates",
    "Identidad": "Identity",
    "Presentá tu marca": "Introduce your brand",
    "Esta información protagoniza la página de invitación y se reutiliza en todas tus campañas.": "This information is featured on the invitation page and reused across all your campaigns.",
    "Identidad del comerciante": "Merchant identity",
    "Nombre visible": "Display name",
    "La marca, no el nombre interno de la campaña.": "The brand, not the campaign’s internal name.",
    "Lema": "Tagline",
    "Una frase corta que explique tu universo.": "A short phrase that explains your world.",
    "Logo (URL HTTPS)": "Logo (HTTPS URL)",
    "PNG, JPG o WebP público. Si lo dejás vacío, usamos las iniciales.": "Public PNG, JPG, or WebP. If left blank, we use the initials.",
    "Continuar al programa →": "Continue to program →",
    "Condiciones": "Terms",
    "Definí el programa": "Define the program",
    "Estos términos quedan asociados a la campaña y respaldados por su prueba Nostr.": "These terms are associated with the campaign and backed by its Nostr proof.",
    "Nombre del programa": "Program name",
    "Comisión por venta (%)": "Commission per sale (%)",
    "Ejemplo: 8 equivale al 8%.": "Example: 8 equals 8%.",
    "Ventana de atribución (días)": "Attribution window (days)",
    "Se aplica internamente; no aparece en el encabezado destacado de la invitación.": "Applied internally; it does not appear in the invitation’s highlighted header.",
    "URL de destino": "Destination URL",
    "URL de términos": "Terms URL",
    "← Volver": "← Back",
    "Continuar a la invitación →": "Continue to invitation →",
    "Conversión": "Conversion",
    "Prepará la invitación": "Prepare the invitation",
    "Personalizá el mensaje de inscripción. Si dejás el texto vacío, usamos una versión predeterminada basada en tu marca.": "Customize the enrollment message. If left blank, we use a default version based on your brand.",
    "Antetítulo": "Eyebrow",
    "Programa de afiliados · Valor por valor": "Affiliate program · Value for value",
    "Título principal": "Main headline",
    "Recomendá café. Ganá sats.": "Recommend coffee. Earn sats.",
    "Descripción": "Description",
    "Crear programa y terminar": "Create program and finish",
    "Al terminar se crea la campaña activa y queda lista para generar invitaciones.": "When finished, the active campaign is created and ready to generate invitations.",
    "Comerciante · Meerat": "Merchant · Meerat",
    "Afiliado · Meerat": "Affiliate · Meerat",
    "Resumen": "Overview",
    "Afiliados": "Affiliates",
    "Actividad": "Activity",
    "Pagos": "Payments",
    "Configuración": "Settings",
    "Tu programa, bajo control.": "Your program, under control.",
    "El estado de tu programa, las próximas acciones y sus resultados en un solo lugar.": "Your program status, next actions, and results in one place.",
    "Campañas": "Campaigns",
    "Condiciones públicas, estado y rendimiento de tus programas de afiliados.": "Public terms, status, and performance of your affiliate programs.",
    "Comunidad": "Community",
    "Invitá personas y administrá las identidades inscritas en tus campañas.": "Invite people and manage the identities enrolled in your campaigns.",
    "Analítica": "Analytics",
    "Clics, conversiones y comisiones confirmadas desde tus enlaces.": "Clicks, conversions, and commissions confirmed through your links.",
    "Obligaciones de pago sin custodia y evidencia verificable de liquidación.": "Non-custodial payment obligations and verifiable settlement evidence.",
    "Comercio": "Commerce",
    "Integración Shopify": "Shopify integration",
    "Seguimiento, píxel y webhook firmado para una atribución verificable.": "Tracking, pixel, and signed webhook for verifiable attribution.",
    "Marca e invitación": "Brand and invitation",
    "Actualizá la identidad pública del comerciante y el mensaje de cada campaña.": "Update the merchant’s public identity and each campaign’s message.",
    "Ver integración": "View integration",
    "Métricas del comerciante": "Merchant metrics",
    "Campañas activas": "Active campaigns",
    "programas": "programs",
    "npubs inscritos": "enrolled npubs",
    "Clics": "Clicks",
    "redirecciones registradas": "recorded redirects",
    "Conversiones": "Conversions",
    "compras aprobadas": "approved purchases",
    "Compras Shopify": "Shopify purchases",
    "Comisiones": "Commissions",
    "sats generados": "sats generated",
    "Próximas acciones": "Next actions",
    "Entrá directo al flujo que necesitás operar.": "Go directly to the workflow you need.",
    "Invitar afiliados": "Invite affiliates",
    "Generá un enlace privado de inscripción.": "Generate a private enrollment link.",
    "Revisar actividad": "Review activity",
    "Actualizar marca": "Update brand",
    "Editá la identidad y el texto de la invitación.": "Edit the identity and invitation text.",
    "Invitar a un afiliado": "Invite an affiliate",
    "Generá un enlace privado. La identidad queda asociada recién cuando el afiliado acepta y firma.": "Generate a private link. The identity is associated only after the affiliate accepts and signs.",
    "Campaña": "Campaign",
    "No hay campañas activas": "There are no active campaigns",
    "Validez del enlace": "Link validity",
    "3 días": "3 days",
    "7 días": "7 days",
    "14 días": "14 days",
    "30 días": "30 days",
    "Generar invitación": "Generate invitation",
    "Copiar invitación": "Copy invitation",
    "Marca e invitación para afiliados": "Brand and affiliate invitation",
    "Vista previa": "Preview",
    "Nombre visible del comerciante": "Merchant display name",
    "Texto de la invitación": "Invitation text",
    "Lo que verá el afiliado antes de firmar.": "What the affiliate will see before signing.",
    "Antetítulo de la invitación": "Invitation eyebrow",
    "Guardar cambios": "Save changes",
    "Los cambios se aplican también a invitaciones existentes.": "Changes also apply to existing invitations.",
    "Actividad y condiciones del programa.": "Program activity and terms.",
    "afiliados": "affiliates",
    "conversiones": "conversions",
    "Página pública": "Public page",
    "Afiliados inscritos": "Enrolled affiliates",
    "Todos los npubs vinculados a tus programas.": "All npubs linked to your programs.",
    "Código de referencia": "Referral code",
    "Copiar npub": "Copy npub",
    "Todavía no hay npubs inscritos.": "There are no enrolled npubs yet.",
    "Últimas redirecciones registradas desde enlaces de afiliados.": "Latest redirects recorded from affiliate links.",
    "ID del clic": "Click ID",
    "Afiliado": "Affiliate",
    "Destino no informado": "Destination not provided",
    "Todavía no hay clics registrados.": "There are no recorded clicks yet.",
    "Actividad reciente": "Recent activity",
    "Venta": "Sale",
    "Comisión": "Commission",
    "Recibo": "Receipt",
    "Todavía no hay conversiones atribuidas.": "There are no attributed conversions yet.",
    "Obligaciones de pago": "Payment obligations",
    "Estado financiero; no implica que Meerat custodie fondos.": "Financial status; it does not imply that Meerat holds funds in custody.",
    "El afiliado debe configurar su dirección Lightning.": "The affiliate must configure their Lightning address.",
    "Estado": "Status",
    "Creado": "Created",
    "Pagar / registrar": "Pay / record",
    "Generar factura Lightning y QR": "Generate Lightning invoice and QR",
    "Factura Lightning": "Lightning invoice",
    "Copiar BOLT11": "Copy BOLT11",
    "Ya pagué · cargar el hash de la factura": "I have paid · load the invoice hash",
    "Generar la factura no realiza ni verifica el pago. Pagala desde tu billetera antes de cargar su hash.": "Generating the invoice does not make or verify the payment. Pay it from your wallet before loading its hash.",
    "Tipo de evidencia": "Evidence type",
    "Hash de pago Lightning (64 caracteres hexadecimales)": "Lightning payment hash (64 hexadecimal characters)",
    "Confirmar liquidación": "Confirm settlement",
    "No hay obligaciones de pago registradas.": "There are no recorded payment obligations.",
    "Script del tema de Shopify": "Shopify theme script",
    "Copiar script": "Copy script",
    "Píxel personalizado de Shopify": "Shopify custom pixel",
    "Copiar píxel": "Copy pixel",
    "Copiar URL": "Copy URL",
    "Conectado": "Connected",
    "Requiere atención": "Needs attention",
    "Activa": "Active",
    "Pausada": "Paused",
    "Finalizada": "Ended",
    "Pendiente": "Pending",
    "Aprobada": "Approved",
    "Rechazada": "Rejected",
    "Pagado": "Paid",
    "Fallido": "Failed",
    "En espera": "On hold",
    "Listo para pagar": "Ready to pay",
    "Procesando": "Processing",
    "Requiere revisión": "Needs review",
    "Pagado y publicado": "Paid and published",
    "Cancelado": "Cancelled",
    "Configuración inicial del afiliado · Meerat": "Affiliate setup · Meerat",
    "Configuración inicial del afiliado": "Affiliate setup",
    "Falta destino de cobro": "Payment destination missing",
    "Configurar cobro": "Set up payments",
    "Cuenta de afiliado": "Affiliate account",
    "Destino verificado": "Verified destination",
    "Mis enlaces": "My links",
    "Ganancias": "Earnings",
    "Cobros": "Payments",
    "Tus resultados, sin mezclar tareas.": "Your results, with each task kept separate.",
    "Una lectura rápida de tus enlaces, conversiones y sats; cada operación vive en su propia vista.": "A quick look at your links, conversions, and sats; each operation has its own view.",
    "Distribución": "Distribution",
    "Programas aceptados y enlaces canónicos listos para compartir.": "Accepted programs and canonical links ready to share.",
    "Comisiones atribuidas, estado de liquidación y recibos verificables.": "Attributed commissions, settlement status, and verifiable receipts.",
    "Resultados": "Results",
    "Ventas confirmadas desde tus enlaces de referencia y sus pruebas públicas.": "Confirmed sales from your referral links and their public proofs.",
    "Destino Lightning": "Lightning destination",
    "La dirección LNURL-pay verificada que usarán los comerciantes para pagarte.": "The verified LNURL-pay address merchants will use to pay you.",
    "Configuración inicial · Cobros": "Initial setup · Payments",
    "Antes de compartir, asegurá cómo cobrar.": "Before sharing, make sure you are ready to get paid.",
    "Registrá una dirección Lightning válida. La verificamos contra LNURL-pay y la asociamos a todos tus programas desde el principio.": "Add a valid Lightning address. We verify it through LNURL-pay and associate it with all your programs from the start.",
    "Destino de cobro": "Payment destination",
    "Tu identidad recomienda. Tu billetera cobra.": "Your identity recommends. Your wallet gets paid.",
    "Meerat no custodia fondos ni necesita acceso a tu billetera. El comerciante genera la factura Lightning directamente desde esta dirección cuando llega el momento de pagarte.": "Meerat does not custody funds or need access to your wallet. When it is time to pay you, the merchant generates the Lightning invoice directly from this address.",
    "Verificamos LNURL-pay": "We verify LNURL-pay",
    "La dirección debe existir y responder como un destino Lightning cobrable.": "The address must exist and respond as a payable Lightning destination.",
    "La propagamos a tus programas": "We propagate it to your programs",
    "Tus inscripciones y pagos elegibles quedan vinculados al mismo destino.": "Your enrollments and eligible payments are linked to the same destination.",
    "Seguís manteniendo la custodia de tus fondos": "You retain custody of your funds",
    "Los sats viajan del comerciante a tu billetera; Meerat conserva únicamente evidencia.": "The sats travel from the merchant to your wallet; Meerat retains only evidence.",
    "Dirección Lightning": "Lightning address",
    "¿Dónde querés recibir tus sats?": "Where do you want to receive your sats?",
    "Usá una dirección de una billetera que controles.": "Use an address from a wallet you control.",
    "Tu dirección Lightning": "Your Lightning address",
    "Ejemplo:": "Example:",
    "No pegues una factura BOLT11.": "Do not paste a BOLT11 invoice.",
    "Privado por diseño": "Private by design",
    "Tu dirección no aparece en páginas públicas, pruebas ni en la API de inscripciones.": "Your address does not appear on public pages, in proofs, or in the enrollment API.",
    "Verificar y entrar al panel →": "Verify and open dashboard →",
    "Haremos una consulta segura al dominio de tu billetera. No se envían sats durante esta verificación.": "We will make a secure request to your wallet's domain. No sats are sent during this verification.",
    "Ver mis enlaces": "View my links",
    "Resumen del afiliado": "Affiliate overview",
    "Enlaces activos": "Active links",
    "programas listos": "programs ready",
    "visitas atribuidas": "attributed visits",
    "ventas confirmadas": "confirmed sales",
    "Comisión bruta": "Gross commission",
    "Listo para cobrar desde el primer enlace.": "Ready to get paid from your very first link.",
    "Administrar destino": "Manage destination",
    "Tu flujo como afiliado": "Your affiliate workflow",
    "Cada tarea tiene su lugar, para que puedas compartir, medir y cobrar sin mezclar información.": "Each task has its own place, so you can share, measure, and get paid without mixing information.",
    "Compartir enlaces": "Share links",
    "Copiá el enlace canónico de cada programa activo.": "Copy the canonical link for each active program.",
    "Revisar conversiones": "Review conversions",
    "Seguimiento de ventas y pruebas públicas.": "Track sales and public proofs.",
    "Seguir tus sats": "Track your sats",
    "Comisiones, estado de pago y recibos.": "Commissions, payment status, and receipts.",
    "Tu enlace para compartir": "Your link to share",
    "Todavía no tenés enlaces": "You do not have any links yet",
    "Usá el enlace canónico del programa; cada visita crea un clic nuevo sin cambiar tu código de referencia.": "Use the program's canonical link; each visit creates a new click without changing your referral code.",
    "Abrí una invitación del comerciante y firmala con esta misma identidad Nostr.": "Open a merchant invitation and sign it with this same Nostr identity.",
    "Enlace": "Link",
    "Copiar": "Copy",
    "Esperá a que el comerciante active el programa antes de compartirlo.": "Wait for the merchant to activate the program before sharing it.",
    "Abrir visita de prueba": "Open test visit",
    "Listo para compartir": "Ready to share",
    "Programa pausado": "Program paused",
    "Falta destino verificado": "Verified destination required",
    "Acceso pendiente": "Access pending",
    "Resumen de ganancias": "Earnings overview",
    "sats atribuidos": "attributed sats",
    "sats liquidados": "settled sats",
    "obligaciones registradas": "recorded obligations",
    "Ganancias y pagos": "Earnings and payments",
    "Comisiones atribuidas y su estado de liquidación.": "Attributed commissions and their settlement status.",
    "Hash de pago": "Payment hash",
    "Todavía no hay pagos asociados a tus conversiones.": "There are no payments associated with your conversions yet.",
    "Conversiones recientes": "Recent conversions",
    "Resultados vinculados a tus enlaces de referencia.": "Results linked to your referral links.",
    "Prueba": "Proof",
    "Tus primeras conversiones aparecerán acá.": "Your first conversions will appear here.",
    "La dirección que usarán los comerciantes para pagarte directamente.": "The address merchants will use to pay you directly.",
    "LNURL-pay verificado": "LNURL-pay verified",
    "Si la cambiás, actualizamos inscripciones y pagos seguros todavía no intentados.": "If you change it, we update enrollments and eligible payments that have not been attempted yet.",
    "Verificar y actualizar": "Verify and update",
    "Volveremos a verificar LNURL-pay antes de guardar. Meerat no recibe ni custodia estos fondos.": "We will verify LNURL-pay again before saving. Meerat does not receive or custody these funds.",
    "Privacidad": "Privacy",
    "Tu destino no es público": "Your destination is not public",
    "Se comparte únicamente dentro del flujo autenticado de pago. No se expone en recibos públicos ni representaciones de la inscripción.": "It is shared only within the authenticated payment flow. It is not exposed in public receipts or enrollment representations.",
    "Verificado": "Verified",
    "Destino vigente": "Current destination",
    "Compartí el enlace únicamente con la persona que querés invitar. No hace falta pedirle su npub.": "Share the link only with the person you want to invite. You do not need to ask for their npub.",
    "Fragmentos de código listos para copiar, pegar y conectar Lightning Koffee.": "Code snippets ready to copy, paste, and connect Lightning Koffee.",
    "La integración de Shopify todavía no está configurada para este comerciante.": "The Shopify integration has not yet been configured for this merchant.",
    "Seguimiento del sitio": "Site tracking",
    "Instalación no disponible para esta identidad.": "Installation is not available for this identity.",
    "Solo la cuenta de comerciante vinculada a la tienda Shopify puede ver y copiar estos fragmentos de código.": "Only the merchant account linked to the Shopify store can view and copy these code snippets.",
    "Personalizá la identidad del comerciante y el texto de cada campaña. Estos valores se definieron durante la configuración inicial y podés actualizarlos cuando quieras.": "Customize the merchant identity and each campaign's copy. These values were defined during setup and can be updated at any time.",
    "Ver página pública ↗": "View public page ↗",
    "La marca protagonista de la página de invitación.": "The brand featured on the invitation page.",
    "Una frase corta sobre tu marca.": "A short phrase about your brand.",
    "Dejá los campos de texto vacíos para usar el texto predeterminado.": "Leave the text fields blank to use the default copy.",
    "Tu destino verificado es": "Your verified destination is",
    ". Los comerciantes pueden generar facturas Lightning LNURL-pay sin que Meerat custodie tus fondos.": ". Merchants can generate Lightning LNURL-pay invoices without Meerat taking custody of your funds.",
    "La dirección que usarán los Comerciantes para pagarte directamente.": "The address merchants will use to pay you directly.",
    "Esperando primer evento": "Waiting for first event",
    "Café, Bitcoin y comunidad": "Coffee, Bitcoin, and community",
}

# Merchant workspace coverage. Keep application-owned copy here; merchant-authored
# names, taglines, destinations, and invitation text must remain untouched.
ES_TO_EN.update({
    "Clics, conversiones y comisiones.": "Clicks, conversions, and commissions.",
    "Revisá destino y monto, pagá desde tu billetera y registrá la evidencia sin ceder custodia a Meerat.": "Review the destination and amount, pay from your wallet, and record the evidence without giving Meerat custody of funds.",
    "Configurado; esperando el primer webhook orders/paid válido.": "Configured; waiting for the first valid orders/paid webhook.",
    "No hay campañas privadas activas": "There are no active private campaigns",
    "PNG, JPG o WebP público. Si falta, usamos las iniciales.": "Public PNG, JPG, or WebP. If omitted, we use the initials.",
    "Sumate al programa de afiliados…": "Join the affiliate program…",
    "Solo invitación": "Invitation only",
    "Requiere aprobación": "Approval required",
    "Inscripción abierta": "Open enrollment",
    "Modo": "Mode",
    "Privada": "Private",
    "Con aprobación": "Approval required",
    "Abierta": "Open",
    "Guardar modo": "Save mode",
    "Comisión (%)": "Commission (%)",
    "Guardar comisión": "Save commission",
    "Comisión actualizada.": "Commission updated.",
    "Copiar inscripción": "Copy enrollment link",
    "No hay campañas asociadas a esta cuenta de comerciante.": "There are no campaigns associated with this merchant account.",
    "Solicitudes pendientes": "Pending applications",
    "Revisá cada identidad antes de activar su enlace.": "Review each identity before activating its link.",
    "Aprobar": "Approve",
    "Rechazar": "Reject",
    "Npubs aprobados y vinculados a tus programas.": "Approved npubs linked to your programs.",
    "Conversiones y comisiones confirmadas.": "Confirmed conversions and commissions.",
    "Preimagen (64 caracteres hexadecimales; se calcula el hash localmente)": "Preimage (64 hexadecimal characters; the hash is calculated locally)",
    "Evidencia Lightning (64 caracteres hex)": "Lightning evidence (64 hexadecimal characters)",
    "64 caracteres: 0-9 y a-f": "64 characters: 0-9 and a-f",
    "No pegues el ID UUID de Strike. Para la factura generada arriba, usá “cargar el hash de la factura”.": "Do not paste the Strike UUID. For the invoice generated above, use “load invoice hash.”",
    "Esto registra una declaración firmada del comerciante.": "This records a merchant-signed declaration.",
    "Webhook orders/paid de Shopify": "Shopify orders/paid webhook",
    "Recibiendo eventos": "Receiving events",
    "El píxel registra evidencia del navegador. El webhook firmado sigue siendo la fuente autoritativa para comisiones y pagos.": "The pixel records browser evidence. The signed webhook remains the authoritative source for commissions and payments.",
    "Tienda configurada:": "Configured store:",
    "¿Quién puede sumarse?": "Who can join?",
    "Privada · Solo con invitación": "Private · Invitation only",
    "Compartís enlaces únicos y de un solo uso.": "You share unique, one-time links.",
    "Con aprobación · Revisás cada solicitud": "Approval required · Review each application",
    "La campaña es pública y vos aprobás cada Affiliate.": "The campaign is public, and you approve each affiliate.",
    "Abierta · Inscripción inmediata": "Open · Immediate enrollment",
    "Cualquier persona puede firmar con Nostr y sumarse.": "Anyone can sign with Nostr and join.",
    "Podés cambiar el modo más adelante. Las inscripciones existentes no se modifican.": "You can change the mode later. Existing enrollments will not be changed.",
    "Modo actualizado.": "Mode updated.",
    "Affiliate aprobado.": "Affiliate approved.",
    "Solicitud rechazada.": "Application rejected.",
    "Creando programa y guardando su prueba Nostr…": "Creating program and saving its Nostr proof…",
    "Guardando marca, programa e invitación…": "Saving brand, program, and invitation…",
    "Programa listo. Abriendo tu resumen…": "Program ready. Opening your overview…",
    "Programa creado. Cargando condiciones…": "Program created. Loading terms…",
    "Guardando identidad de marca…": "Saving brand identity…",
    "Guardando el texto de la invitación…": "Saving invitation copy…",
    "Marca e invitación guardadas. Actualizando la vista…": "Brand and invitation saved. Refreshing the view…",
    "Generando enlace privado de un solo uso…": "Generating a private one-time link…",
    "El servidor devolvió un enlace inválido": "The server returned an invalid link.",
    "Generá nuevamente la factura Lightning antes de registrar el pago.": "Generate the Lightning invoice again before recording the payment.",
    "Esta factura Lightning expiró. Generá una nueva antes de pagar.": "This Lightning invoice has expired. Generate a new one before paying.",
    "Hash de la factura cargado. Confirmá abajo únicamente si tu billetera mostró que el pago fue exitoso.": "Invoice hash loaded. Confirm below only if your wallet showed that the payment succeeded.",
    "Resolviendo la dirección Lightning y generando la factura…": "Resolving the Lightning address and generating the invoice…",
    "El servidor devolvió una factura Lightning inválida": "The server returned an invalid Lightning invoice.",
    "Regenerar factura Lightning y QR": "Regenerate Lightning invoice and QR code",
    "Ingresá el hash de pago Lightning: exactamente 64 caracteres hexadecimales, sin guiones. El ID UUID de Strike no es un hash de pago.": "Enter the Lightning payment hash: exactly 64 hexadecimal characters, without hyphens. A Strike UUID is not a payment hash.",
    "Marcá la confirmación únicamente si tu billetera mostró que el pago fue exitoso.": "Check the confirmation only if your wallet showed that the payment succeeded.",
    "Registrando atestación y publicando prueba…": "Recording attestation and publishing proof…",
    "Pago registrado.": "Payment recorded.",
    "No hay contenido para copiar": "There is no content to copy.",
    "Copiado": "Copied",
    "Contenido copiado": "Content copied",
    "Copiá manualmente": "Copy manually",
    "No se pudo copiar automáticamente": "Could not copy automatically",
    "NostrKey no devolvió un evento firmado. Verificá que esté desbloqueado y aprobá la solicitud de firma.": "NostrKey did not return a signed event. Make sure it is unlocked and approve the signing request.",
    "No se pudo cargar la conexión con la app Nostr. Recargá la página e intentá nuevamente.": "The Nostr app connection could not be loaded. Reload the page and try again.",
    "La conexión con la app Nostr no está disponible.": "The Nostr app connection is unavailable.",
    "Método de firma no válido.": "Invalid signing method.",
    "Esta campaña usa una página pública de inscripción y no necesita invitaciones privadas.": "This campaign uses a public enrollment page and does not need private invitations.",
    "No se encontró la solicitud.": "Application not found.",
    "La solicitud fue modificada simultáneamente.": "The application was modified concurrently.",
    "Cerrar": "Close",
})


# Error messages returned by /app APIs and rendered by the frontend.
ES_TO_EN.update({'El afiliado todavía no configuró una dirección Lightning.': 'affiliate has not configured a Lightning '
                                                              'Address',
 'El desafío de autenticación expiró.': 'authentication challenge expired',
 'El desafío de autenticación ya fue utilizado.': 'authentication challenge was already used',
 'El destino de cobro del afiliado no está configurado o verificado.': 'affiliate payout destination is not '
                                                                       'configured or verified',
 'El destino o el monto del pago cambió mientras se preparaba la factura Lightning.': 'payout destination or '
                                                                                      'amount changed while '
                                                                                      'preparing the invoice',
 'El evento de autenticación debe incluir exactamente una etiqueta challenge.': 'authentication event '
                                                                                'requires exactly one '
                                                                                'challenge tag',
 'El hash de pago ya está asignado a otro pago.': 'payment hash is already assigned to another payout',
 'El pago cambió mientras se preparaba la factura Lightning.': 'payout changed while preparing the invoice',
 'El pago fue modificado simultáneamente por otra operación.': 'payout was concurrently modified',
 'El pago no tiene una reserva completa en el presupuesto de la campaña.': 'payout has no complete campaign '
                                                                           'budget reservation',
 'El pago se liquidó con evidencia diferente.': 'payout was settled with different evidence',
 'El pago tiene un período de devolución no válido.': 'payout has an invalid return window',
 'El pago ya está asignado a un proveedor de pagos.': 'payout already belongs to a payment provider',
 'El pago ya tiene evidencia o un intento de pago registrado.': 'payout already has payment evidence or an '
                                                                'attempt',
 'El período de devolución del pago todavía no terminó.': 'payout return window has not ended',
 'El programa ya existe con una configuración diferente.': 'program already exists with different settings',
 'El servicio de autenticación está ocupado. Intentá nuevamente en unos instantes.': 'authentication service '
                                                                                     'is busy; retry shortly',
 'La campaña no está activa.': 'campaign is not active',
 'La configuración de la lista de operadores permitidos no es válida.': 'operator allowlist configuration is '
                                                                        'invalid',
 'La configuración de vinculación de la cuenta del comerciante no es válida.': 'merchant account binding '
                                                                               'configuration is invalid',
 'La configuración predeterminada del programa del comerciante no es válida.': 'merchant default program '
                                                                               'configuration is invalid',
 'La cuenta del afiliado no está activa.': 'affiliate account is not active',
 'La dirección Lightning no es válida.': 'invalid Lightning Address',
 'La dirección Lightning no existe o no ofrece LNURL-pay.': 'The Lightning address does not exist or does not support '
                                                            'LNURL-pay.',
 'La factura BOLT11 es demasiado grande para mostrarla de forma segura.': 'BOLT11 invoice is too large to '
                                                                          'render safely',
 'La identidad no coincide con la del programa predeterminado.': 'default program identity conflict',
 'La invitación aceptada no tiene una inscripción activa.': 'accepted invitation has no active enrollment',
 'La invitación expiró.': 'invitation expired',
 'La invitación fue revocada.': 'invitation was revoked',
 'La invitación ya fue utilizada o revocada.': 'invitation was already used or revoked',
 'La preparación de la factura Lightning ya está en curso o se solicitó hace muy poco.': 'invoice '
                                                                                         'preparation is '
                                                                                         'already running or '
                                                                                         'was requested too '
                                                                                         'recently',
 'La solicitud debe provenir del mismo origen.': 'same-origin request required',
 'No hay una sesión autenticada.': 'not authenticated',
 'No se encontró el comerciante que se quería configurar.': 'merchant bootstrap target not found',
 'No se encontró el desafío de autenticación.': 'authentication challenge not found',
 'No se encontró el pago.': 'payout not found',
 'No se encontró el perfil del comerciante solicitado.': 'merchant profile target not found',
 'No se encontró la campaña.': 'campaign not found',
 'Solo se puede cambiar la comisión de una campaña activa.': 'commission can only be changed for an active campaign',
 'No se encontró la invitación.': 'invitation not found',
 'No se encontró la vista solicitada del espacio del afiliado.': 'affiliate workspace view not found',
 'No se encontró la vista solicitada del espacio del comerciante.': 'merchant workspace view not found',
 'No se puede pagar una conversión revertida.': 'reversed conversion payout is not payable',
 'Otra identidad ya utilizó esta invitación.': 'invitation was already used by another identity',
 'Se solicitaron demasiados desafíos de autenticación. Intentá nuevamente en unos instantes.': 'too many '
                                                                                               'authentication '
                                                                                               'challenges; '
                                                                                               'retry '
                                                                                               'shortly',
 'logo_url debe usar el puerto HTTPS estándar.': 'logo_url must use the standard HTTPS port',
 'logo_url debe usar un host público.': 'logo_url must use a public host',
 'logo_url no admite imágenes SVG.': 'logo_url SVG images are not supported',
 'program_name es obligatorio.': 'program_name is required'})

_DYNAMIC = [
    (re.compile(r"^de (\d+) programas$"), lambda match: f"out of {match.group(1)} {'program' if match.group(1) == '1' else 'programs'}"),
    (re.compile(r"^(\d+) eventos asociados a tus campañas\.$"), lambda match: f"{match.group(1)} {'event' if match.group(1) == '1' else 'events'} associated with your campaigns."),
    (re.compile(r"^(\d+) webhooks? de orders/paid aprobados?$"), lambda match: f"{match.group(1)} approved orders/paid {'webhook' if match.group(1) == '1' else 'webhooks'}"),
    (re.compile(r"^(\d+) pagos? requieren? atención\.$"), lambda match: f"{match.group(1)} {'payment needs' if match.group(1) == '1' else 'payments need'} attention."),
    (re.compile(r"^(.+)% · ventana (.+) días$"), r"\1% · \2-day attribution window"),
    (re.compile(r"^(\d+) pendientes?$"), r"\1 pending"),
    (re.compile(r"^(\d+) total$"), r"Total: \1"),
    (re.compile(r"^Liquidable después de (.+)$"), r"Payable after \1"),
    (re.compile(r"^La factura Lightning se genera directamente desde (.+)\.$"), r"The Lightning invoice is generated directly from \1."),
    (re.compile(r"^QR de la factura Lightning para (\d+) sats$"), r"Lightning invoice QR code for \1 sats"),
    (re.compile(r"^Confirmo que pagué (\d+) sats a (.+)$"), r"I confirm that I paid \1 sats to \2"),
    (re.compile(r"^(\d+) webhooks? orders/paid procesados?\.$"), lambda match: f"{match.group(1)} orders/paid {'webhook' if match.group(1) == '1' else 'webhooks'} processed."),
    (re.compile(r"^Logo de (.+)$"), r"\1 logo"),
    (re.compile(r"^La solicitud ya tiene el estado (.+)\.$"), r"The application is already in the \1 state."),
    (re.compile(r"^(\d+) sats pagados$"), r"\1 sats paid"),
    (re.compile(r"^(\d+) activos$"), r"\1 active links"),
    (re.compile(r"^(\d+) activo$"), r"\1 active link"),
    (re.compile(r"^(\d+) aprobadas$"), r"\1 approved conversions"),
    (re.compile(r"^(\d+) aprobada$"), r"\1 approved conversion"),
    (re.compile(r"^Comerciante (.+) · (.+)% · ventana (.+) días$"), r"Merchant \1 · \2% · \3-day window"),
    (re.compile(r"^Validación LNURL-pay completada el (.+) \(UTC\)\.$"), r"LNURL-pay verification completed on \1 (UTC)."),
    (re.compile(r"^No se pudo cerrar sesión: (.+)$"), r"Could not sign out: \1"),
]


def resolve_language(query: str | None, cookie: str | None, accept_language: str | None) -> str:
    if query in SUPPORTED_LANGUAGES:
        return query
    if cookie in SUPPORTED_LANGUAGES:
        return cookie
    for item in (accept_language or "").split(","):
        code = item.split(";", 1)[0].strip().lower().split("-", 1)[0]
        if code in SUPPORTED_LANGUAGES:
            return code
    return DEFAULT_LANGUAGE


def translate_text(value: str, language: str) -> str:
    if language != "en":
        return value
    translated = ES_TO_EN.get(value)
    if translated is not None:
        return translated
    for pattern, replacement in _DYNAMIC:
        if pattern.match(value):
            return pattern.sub(replacement, value)
    return value


class _HTMLTranslator(HTMLParser):
    _translated_attributes = {"aria-label", "placeholder", "title", "alt"}
    _raw_tags = {"script", "style", "code", "pre", "textarea"}
    _void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, language: str):
        super().__init__(convert_charrefs=False)
        self.language = language
        self.parts: list[str] = []
        self.raw_depth = 0
        self.ignore_stack: list[tuple[str, bool]] = []

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        ignored = (self.ignore_stack[-1][1] if self.ignore_stack else False) or any(
            name == "data-i18n-ignore" for name, _ in attrs
        )
        if tag not in self._void_tags:
            self.ignore_stack.append((tag, ignored))
        if tag in self._raw_tags:
            self.raw_depth += 1
        if ignored or not any(name in self._translated_attributes for name, _ in attrs):
            self.parts.append(raw)
            return
        for name, value in attrs:
            if name not in self._translated_attributes or value is None:
                continue
            translated = translate_text(value, self.language)
            if translated != value:
                raw = raw.replace(f'{name}="{value}"', f'{name}="{translated}"')
                raw = raw.replace(f"{name}='{value}'", f"{name}='{translated}'")
        self.parts.append(raw)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if tag in self._raw_tags:
            self.raw_depth = max(0, self.raw_depth - 1)
        if self.ignore_stack and self.ignore_stack[-1][0] == tag:
            self.ignore_stack.pop()

    def handle_data(self, data: str) -> None:
        ignored = self.ignore_stack[-1][1] if self.ignore_stack else False
        if ignored or self.raw_depth or not data.strip():
            self.parts.append(data)
            return
        leading = data[: len(data) - len(data.lstrip())]
        trailing = data[len(data.rstrip()):]
        core = data.strip()
        self.parts.append(leading + translate_text(core, self.language) + trailing)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")


def translate_html(html: str, language: str) -> str:
    if language != "en":
        return html
    parser = _HTMLTranslator(language)
    parser.feed(html)
    parser.close()
    result = "".join(parser.parts)
    return result.replace('<html lang="es">', '<html lang="en">')


def javascript_catalog(language: str) -> dict[str, str]:
    return ES_TO_EN if language == "en" else {}
